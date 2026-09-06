from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / "workflows" / "product-release-admission-proof.yml"


def test_release_resolver_is_sole_android_package_authority() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    for required in (
        "from release_resolver import resolve_release",
        "resolve_release(tag=release_tag, target=target)",
        '"android_package": admitted.android_package',
        "Android package: {admitted.android_package}",
    ):
        assert required in source

    assert re.search(r"(?m)^\s*if\s+.*android_package", source) is None
    assert re.search(r"android_package\s*(?:==|!=)", source) is None

    package_literals = re.findall(
        r"(?<![A-Za-z0-9_])(?:[a-z][a-z0-9_]*\.){2,}[A-Za-z0-9_]+",
        source,
    )
    assert package_literals == [], package_literals


def main() -> int:
    test_release_resolver_is_sole_android_package_authority()
    print("PRODUCT_RELEASE_ADMISSION_WRAPPER_AUTHORITY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
