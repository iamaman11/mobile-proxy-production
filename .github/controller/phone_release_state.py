from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from android_target import AndroidObservationUnavailable, observe
from phone_runtime import PhoneRuntimeRefused, materialize_runtime_bundle
from phone_target import PhoneTargetUnavailable, observe_runtime
from product_runtime_renderer import (
    bind_renderer_inputs,
    render_required_runtime_configs,
    verify_product_source,
    verify_release_component_digests,
)
from runtime_asset_cache import RuntimeAssetCacheError, get_or_fetch_runtime_archive

_SHA256 = re.compile(r"[0-9a-f]{64}")
_RELEASE_URL_PREFIX = "https://github.com/iamaman11/mobile-proxy/releases/download/"
_MAX_RELEASE_ASSET_BYTES = 250 * 1024 * 1024
_RELEASE_ASSET_DOWNLOAD_ATTEMPTS = 3
_RUNTIME_SECRET_FIELDS = (
    "adminTokenEnv",
    "deviceTokenEnv",
    "uiTokenEnv",
    "relayUserEnv",
    "relayPasswordEnv",
    "reverseTunnelCertDerB64Env",
)
_CLASSIFICATIONS = frozenset({"HEALTHY_EXACT", "DEGRADED", "UNKNOWN"})
PHONE_RUNTIME_PREPARATION_TIMING_FIELDS = (
    "runtime_archive_acquisition",
    "immutable_static_materialization",
    "product_source_verification",
    "product_component_verification",
    "renderer_input_binding",
    "runtime_manifest_validation",
    "secret_derived_rendering",
    "secret_binding_derivation",
    "total",
)


@dataclass(frozen=True)
class PhoneReleaseSnapshot:
    """Concrete current-state result for one admitted phone Product Release."""

    classification: str
    apk: object | None
    runtime: object | None
    failure: BaseException | None = None

    def __post_init__(self) -> None:
        if self.classification not in _CLASSIFICATIONS:
            raise ValueError("phone Release snapshot classification is invalid")
        if self.classification == "UNKNOWN":
            if self.apk is not None or self.runtime is not None or self.failure is None:
                raise ValueError("UNKNOWN phone Release snapshot shape is invalid")
        elif self.apk is None or self.runtime is None or self.failure is not None:
            raise ValueError("observed phone Release snapshot shape is invalid")

    @property
    def desired(self) -> bool:
        return self.classification == "HEALTHY_EXACT"


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((time.monotonic() - started_at) * 1000))


def _download_release_asset(
    *,
    url: str,
    destination: Path,
    expected_transport_sha256: str,
    label: str,
) -> None:
    if not url.startswith(_RELEASE_URL_PREFIX):
        raise PhoneRuntimeRefused(f"{label} Release asset URL differs")
    if _SHA256.fullmatch(expected_transport_sha256) is None:
        raise PhoneRuntimeRefused(f"{label} Release transport digest is invalid")
    for attempt in range(_RELEASE_ASSET_DOWNLOAD_ATTEMPTS):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "mobile-proxy-production-controller-v2"},
        )
        digest = hashlib.sha256()
        total = 0
        try:
            with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_RELEASE_ASSET_BYTES:
                        raise PhoneRuntimeRefused(f"{label} Release asset exceeds size bound")
                    digest.update(chunk)
                    handle.write(chunk)
        except PhoneRuntimeRefused:
            raise
        except (OSError, urllib.error.URLError) as exc:
            if attempt + 1 >= _RELEASE_ASSET_DOWNLOAD_ATTEMPTS:
                raise PhoneRuntimeRefused(f"{label} Release asset download failed") from exc
            continue
        if total <= 0 or not hmac.compare_digest(digest.hexdigest(), expected_transport_sha256):
            raise PhoneRuntimeRefused(f"{label} Release transport digest differs after download")
        return
    raise AssertionError("bounded Release asset download attempts exhausted without terminal classification")


def _read_runtime_manifest(path: Path) -> tuple[str, Mapping[str, object]]:
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PhoneRuntimeRefused("phone production runtime manifest is unavailable") from exc
    if not isinstance(value, Mapping):
        raise PhoneRuntimeRefused("phone production runtime manifest is not an object")
    if value.get("operatorProfile") not in {"a1_by", "default", "mts_by"}:
        raise PhoneRuntimeRefused("phone production runtime operator profile is unsupported")
    tokens = value.get("tokens")
    if not isinstance(tokens, Mapping):
        raise PhoneRuntimeRefused("phone production runtime token mapping is unavailable")
    for field in _RUNTIME_SECRET_FIELDS:
        name = tokens.get(field)
        if not isinstance(name, str) or not name or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise PhoneRuntimeRefused(f"runtime secret environment mapping is invalid: {field}")
    return raw, value


def _runtime_secret_binding_ids(
    manifest: Mapping[str, object],
    *,
    environment: Mapping[str, str],
    binding_key: str,
) -> dict[str, str]:
    tokens = manifest.get("tokens")
    if not isinstance(tokens, Mapping):
        raise PhoneRuntimeRefused("runtime token mapping is unavailable")
    if len(binding_key) < 32:
        raise PhoneRuntimeRefused("runtime credential binding key is unavailable")
    result: dict[str, str] = {}
    for field in _RUNTIME_SECRET_FIELDS:
        env_name = str(tokens[field])
        secret = environment.get(env_name, "")
        if not secret:
            raise PhoneRuntimeRefused(f"required runtime secret is unavailable: {env_name}")
        digest = hmac.new(
            binding_key.encode("utf-8"),
            env_name.encode("utf-8") + b"\0" + secret.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        result[env_name] = "hmac-sha256:" + digest
    return dict(sorted(result.items()))


def _runtime_cache_root() -> Path | None:
    raw = os.environ.get("MOBILE_PROXY_RUNTIME_CACHE_DIR", "").strip()
    return Path(raw) if raw else None


def prepare_verified_release_runtime(
    admitted: object,
    *,
    archive: Path,
    work_root: Path,
    product_root: Path,
    runtime_manifest_path: Path,
    binding_key: str,
    facts: dict[str, object],
):
    """Prepare the exact expected rooted-phone Release state without phone mutation."""

    preparation_started = time.monotonic()
    phase_timing_ms: dict[str, int] = {}
    identity = admitted.identity
    runtime_name = identity.phone_runtime_artifact_name
    runtime_digest = identity.phone_runtime_artifact_digest
    inventory_digest = identity.phone_runtime_inventory_digest
    if (
        not isinstance(runtime_name, str)
        or not runtime_name
        or not isinstance(runtime_digest, str)
        or not isinstance(inventory_digest, str)
        or not admitted.phone_runtime_download_url
        or not admitted.phone_runtime_transport_sha256
    ):
        raise PhoneRuntimeRefused("admitted rooted-phone runtime identity is incomplete")

    expected_transport_sha256 = str(admitted.phone_runtime_transport_sha256)
    cache_root = _runtime_cache_root()
    archive_for_materialization = archive
    cache_state = "disabled"
    phase_started = time.monotonic()
    if cache_root is None:
        _download_release_asset(
            url=str(admitted.phone_runtime_download_url),
            destination=archive,
            expected_transport_sha256=expected_transport_sha256,
            label="rooted-phone runtime",
        )
    else:
        try:
            cached = get_or_fetch_runtime_archive(
                cache_root=cache_root,
                expected_transport_sha256=expected_transport_sha256,
                fetch=lambda destination: _download_release_asset(
                    url=str(admitted.phone_runtime_download_url),
                    destination=destination,
                    expected_transport_sha256=expected_transport_sha256,
                    label="rooted-phone runtime",
                ),
            )
        except RuntimeAssetCacheError as exc:
            raise PhoneRuntimeRefused("runtime archive cache is unavailable") from exc
        archive_for_materialization = cached.path
        cache_state = cached.state
    phase_timing_ms["runtime_archive_acquisition"] = _elapsed_ms(phase_started)

    phase_started = time.monotonic()
    materialized = materialize_runtime_bundle(
        archive_path=archive_for_materialization,
        work_root=work_root,
        expected_transport_sha256=expected_transport_sha256,
    )
    phase_timing_ms["immutable_static_materialization"] = _elapsed_ms(phase_started)

    phase_started = time.monotonic()
    verify_product_source(product_root=product_root, expected_source_sha=identity.source_sha)
    phase_timing_ms["product_source_verification"] = _elapsed_ms(phase_started)

    phase_started = time.monotonic()
    verify_release_component_digests(
        materialized,
        product_root=product_root,
        runtime_archive=archive_for_materialization,
        expected_artifact_name=runtime_name,
        expected_artifact_digest=runtime_digest,
        expected_inventory_digest=inventory_digest,
    )
    phase_timing_ms["product_component_verification"] = _elapsed_ms(phase_started)

    phase_started = time.monotonic()
    bind_renderer_inputs(materialized, product_root=product_root)
    phase_timing_ms["renderer_input_binding"] = _elapsed_ms(phase_started)

    phase_started = time.monotonic()
    manifest_json, manifest = _read_runtime_manifest(runtime_manifest_path)
    phase_timing_ms["runtime_manifest_validation"] = _elapsed_ms(phase_started)
    environment = dict(os.environ)

    phase_started = time.monotonic()
    rendered = render_required_runtime_configs(
        materialized,
        product_root=product_root,
        manifest_json=manifest_json,
        release_id=identity.tag,
        environment=environment,
    )
    phase_timing_ms["secret_derived_rendering"] = _elapsed_ms(phase_started)

    phase_started = time.monotonic()
    secret_bindings = _runtime_secret_binding_ids(
        manifest,
        environment=environment,
        binding_key=binding_key,
    )
    phase_timing_ms["secret_binding_derivation"] = _elapsed_ms(phase_started)
    phase_timing_ms["total"] = _elapsed_ms(preparation_started)

    facts["runtime_verification"] = {
        "exact_release_runtime": True,
        "artifact_name": runtime_name,
        "product_content_digest": runtime_digest,
        "transport_sha256": materialized.transport_sha256,
        "component_inventory_digest": inventory_digest,
        "component_count": len(materialized.components),
        "required_live_file_count": len(materialized.required_live_release_paths),
        "derived_files_rendered": list(rendered),
        "renderer_source_sha": identity.source_sha,
        "runtime_manifest_sha256": hashlib.sha256(manifest_json.encode("utf-8")).hexdigest(),
        "secret_binding_ids": secret_bindings,
        "secret_values_recorded": False,
        "vm_provider_access_performed": False,
        "runtime_archive_cache": {
            "state": cache_state,
            "persistent": cache_root is not None,
            "rendered_trees_retained": False,
        },
        "phase_timing_ms": phase_timing_ms,
    }
    return materialized


def observe_exact_phone_release(
    *,
    serial: str,
    binding_key: str,
    admitted: object,
    materialized: object,
) -> PhoneReleaseSnapshot:
    """Observe exact APK + rooted runtime and classify one fresh current-state snapshot."""

    try:
        apk = observe(
            serial=serial,
            binding_key=binding_key,
            expected_version_name=str(admitted.android_version_name),
            expected_version_code=int(admitted.android_version_code or 0),
            expected_artifact_sha256=admitted.artifact_transport_sha256,
        )
        runtime = observe_runtime(
            serial=serial,
            release_root=materialized.release_root,
            release_id=admitted.identity.tag,
            required_paths=materialized.required_live_release_paths,
        )
    except (AndroidObservationUnavailable, PhoneTargetUnavailable) as exc:
        return PhoneReleaseSnapshot(
            classification="UNKNOWN",
            apk=None,
            runtime=None,
            failure=exc,
        )

    desired = bool(apk.desired and runtime.desired)
    return PhoneReleaseSnapshot(
        classification="HEALTHY_EXACT" if desired else "DEGRADED",
        apk=apk,
        runtime=runtime,
    )


def require_observed_pair(snapshot: PhoneReleaseSnapshot) -> tuple[object, object]:
    """Preserve existing deployment exception semantics at the shared state boundary."""

    if snapshot.classification == "UNKNOWN":
        if snapshot.failure is None:
            raise PhoneTargetUnavailable("exact phone Release observation is unavailable")
        raise snapshot.failure
    if snapshot.apk is None or snapshot.runtime is None:
        raise PhoneTargetUnavailable("exact phone Release observation is incomplete")
    return snapshot.apk, snapshot.runtime
