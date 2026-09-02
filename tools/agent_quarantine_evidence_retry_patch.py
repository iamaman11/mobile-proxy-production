from pathlib import Path

observe_path = Path('.github/workflows/phone-filesystem-quarantine-observation.yml')
cleanup_path = Path('.github/workflows/phone-filesystem-quarantine-cleanup.yml')
policy_path = Path('.github/workflows/filesystem-quarantine-recovery-policy.yml')

observe = observe_path.read_text(encoding='utf-8')
old_observe = '''      - name: Upload bounded quarantine observation evidence
        id: evidence-upload
        if: success()
        continue-on-error: true
        uses: actions/upload-artifact@v4
        with:
          name: phone-filesystem-quarantine-observation-${{ env.CANONICAL_SHA }}-${{ github.run_id }}
          path: ${{ runner.temp }}/phone-filesystem-quarantine-observation.json
          if-no-files-found: error
          retention-days: 30

      - name: Export quarantine observation evidence persistence state
        id: evidence-state
        if: always()
        shell: bash
        env:
          RESULT_OUTCOME: ${{ steps.result.outcome }}
          EVIDENCE_UPLOAD_OUTCOME: ${{ steps.evidence-upload.outcome }}
        run: |
          set -euo pipefail
          persisted=false
          if [ "${RESULT_OUTCOME}" = success ] && [ "${EVIDENCE_UPLOAD_OUTCOME}" = success ]; then
            persisted=true
          fi
          echo "artifact_persisted=${persisted}" >> "$GITHUB_OUTPUT"
'''
new_observe = '''      - name: Upload bounded quarantine observation evidence (attempt 1)
        id: evidence-upload-1
        if: success()
        continue-on-error: true
        uses: actions/upload-artifact@v4
        with:
          name: phone-filesystem-quarantine-observation-${{ env.CANONICAL_SHA }}-${{ github.run_id }}
          path: ${{ runner.temp }}/phone-filesystem-quarantine-observation.json
          if-no-files-found: error
          retention-days: 30

      - name: Upload bounded quarantine observation evidence (attempt 2)
        id: evidence-upload-2
        if: steps.result.outcome == 'success' && steps.evidence-upload-1.outcome != 'success'
        continue-on-error: true
        uses: actions/upload-artifact@v4
        with:
          name: phone-filesystem-quarantine-observation-${{ env.CANONICAL_SHA }}-${{ github.run_id }}
          path: ${{ runner.temp }}/phone-filesystem-quarantine-observation.json
          if-no-files-found: error
          retention-days: 30

      - name: Upload bounded quarantine observation evidence (attempt 3)
        id: evidence-upload-3
        if: steps.result.outcome == 'success' && steps.evidence-upload-1.outcome != 'success' && steps.evidence-upload-2.outcome != 'success'
        continue-on-error: true
        uses: actions/upload-artifact@v4
        with:
          name: phone-filesystem-quarantine-observation-${{ env.CANONICAL_SHA }}-${{ github.run_id }}
          path: ${{ runner.temp }}/phone-filesystem-quarantine-observation.json
          if-no-files-found: error
          retention-days: 30

      - name: Export quarantine observation evidence persistence state
        id: evidence-state
        if: always()
        shell: bash
        env:
          RESULT_OUTCOME: ${{ steps.result.outcome }}
          EVIDENCE_UPLOAD_1_OUTCOME: ${{ steps.evidence-upload-1.outcome }}
          EVIDENCE_UPLOAD_2_OUTCOME: ${{ steps.evidence-upload-2.outcome }}
          EVIDENCE_UPLOAD_3_OUTCOME: ${{ steps.evidence-upload-3.outcome }}
        run: |
          set -euo pipefail
          persisted=false
          if [ "${RESULT_OUTCOME}" = success ] && { [ "${EVIDENCE_UPLOAD_1_OUTCOME}" = success ] || [ "${EVIDENCE_UPLOAD_2_OUTCOME}" = success ] || [ "${EVIDENCE_UPLOAD_3_OUTCOME}" = success ]; }; then
            persisted=true
          fi
          echo "artifact_persisted=${persisted}" >> "$GITHUB_OUTPUT"
'''
if observe.count(old_observe) != 1:
    raise SystemExit('observation upload block drifted')
observe = observe.replace(old_observe, new_observe)
observe_path.write_text(observe, encoding='utf-8')

cleanup = cleanup_path.read_text(encoding='utf-8')
old_cleanup = '''      - name: Upload bounded quarantine cleanup evidence
        id: evidence-upload
        if: always()
        continue-on-error: true
        uses: actions/upload-artifact@v4
        with:
          name: phone-filesystem-quarantine-cleanup-${{ env.CANONICAL_SHA }}-${{ github.run_id }}
          path: ${{ runner.temp }}/phone-filesystem-quarantine-cleanup.json
          if-no-files-found: warn
          retention-days: 30

      - name: Export quarantine cleanup evidence persistence state
        id: evidence-state
        if: always()
        shell: bash
        env:
          RESULT_OUTCOME: ${{ steps.result.outcome }}
          RESULT_VALIDATED: ${{ steps.result.outputs.result_validated }}
          EVIDENCE_UPLOAD_OUTCOME: ${{ steps.evidence-upload.outcome }}
        run: |
          set -euo pipefail
          persisted=false
          if [ "${RESULT_OUTCOME}" = success ] && [ "${RESULT_VALIDATED}" = true ] && [ "${EVIDENCE_UPLOAD_OUTCOME}" = success ]; then
            persisted=true
          fi
          echo "artifact_persisted=${persisted}" >> "$GITHUB_OUTPUT"
'''
new_cleanup = '''      - name: Upload bounded quarantine cleanup evidence (attempt 1)
        id: evidence-upload-1
        if: always()
        continue-on-error: true
        uses: actions/upload-artifact@v4
        with:
          name: phone-filesystem-quarantine-cleanup-${{ env.CANONICAL_SHA }}-${{ github.run_id }}
          path: ${{ runner.temp }}/phone-filesystem-quarantine-cleanup.json
          if-no-files-found: warn
          retention-days: 30

      - name: Upload bounded quarantine cleanup evidence (attempt 2)
        id: evidence-upload-2
        if: always() && steps.evidence-upload-1.outcome != 'success'
        continue-on-error: true
        uses: actions/upload-artifact@v4
        with:
          name: phone-filesystem-quarantine-cleanup-${{ env.CANONICAL_SHA }}-${{ github.run_id }}
          path: ${{ runner.temp }}/phone-filesystem-quarantine-cleanup.json
          if-no-files-found: warn
          retention-days: 30

      - name: Upload bounded quarantine cleanup evidence (attempt 3)
        id: evidence-upload-3
        if: always() && steps.evidence-upload-1.outcome != 'success' && steps.evidence-upload-2.outcome != 'success'
        continue-on-error: true
        uses: actions/upload-artifact@v4
        with:
          name: phone-filesystem-quarantine-cleanup-${{ env.CANONICAL_SHA }}-${{ github.run_id }}
          path: ${{ runner.temp }}/phone-filesystem-quarantine-cleanup.json
          if-no-files-found: warn
          retention-days: 30

      - name: Export quarantine cleanup evidence persistence state
        id: evidence-state
        if: always()
        shell: bash
        env:
          RESULT_OUTCOME: ${{ steps.result.outcome }}
          RESULT_VALIDATED: ${{ steps.result.outputs.result_validated }}
          EVIDENCE_UPLOAD_1_OUTCOME: ${{ steps.evidence-upload-1.outcome }}
          EVIDENCE_UPLOAD_2_OUTCOME: ${{ steps.evidence-upload-2.outcome }}
          EVIDENCE_UPLOAD_3_OUTCOME: ${{ steps.evidence-upload-3.outcome }}
        run: |
          set -euo pipefail
          persisted=false
          if [ "${RESULT_OUTCOME}" = success ] && [ "${RESULT_VALIDATED}" = true ] && { [ "${EVIDENCE_UPLOAD_1_OUTCOME}" = success ] || [ "${EVIDENCE_UPLOAD_2_OUTCOME}" = success ] || [ "${EVIDENCE_UPLOAD_3_OUTCOME}" = success ]; }; then
            persisted=true
          fi
          echo "artifact_persisted=${persisted}" >> "$GITHUB_OUTPUT"
'''
if cleanup.count(old_cleanup) != 1:
    raise SystemExit('cleanup upload block drifted')
cleanup = cleanup.replace(old_cleanup, new_cleanup)
cleanup_path.write_text(cleanup, encoding='utf-8')

policy = policy_path.read_text(encoding='utf-8')
old_contract = '''              "id: evidence-upload",
              "continue-on-error: true",
              "id: evidence-state",'''
new_contract = '''              "id: evidence-upload-1",
              "id: evidence-upload-2",
              "id: evidence-upload-3",
              "continue-on-error: true",
              "id: evidence-state",'''
if policy.count(old_contract) != 2:
    raise SystemExit('policy upload contract anchors drifted')
policy = policy.replace(old_contract, new_contract)

anchor = '''          def classify_observation(validated, persisted, complete, all_absent, admissible):
'''
retry_checks = '''          observation_retry_contract = (
              "id: evidence-upload-1",
              "id: evidence-upload-2",
              "id: evidence-upload-3",
              "steps.evidence-upload-1.outcome != 'success'",
              "steps.evidence-upload-2.outcome != 'success'",
              "EVIDENCE_UPLOAD_1_OUTCOME: ${{ steps.evidence-upload-1.outcome }}",
              "EVIDENCE_UPLOAD_2_OUTCOME: ${{ steps.evidence-upload-2.outcome }}",
              "EVIDENCE_UPLOAD_3_OUTCOME: ${{ steps.evidence-upload-3.outcome }}",
          )
          missing_observation_retry = [item for item in observation_retry_contract if item not in observe]
          if missing_observation_retry:
              raise SystemExit(
                  'quarantine observation persistence retry contract missing: '
                  + ', '.join(missing_observation_retry)
              )
          if observe.count("'--mode', 'observe'") != 1:
              raise SystemExit('quarantine observation device operation must execute exactly once')
          if observe.count('subprocess.run(command, check=True)') != 1:
              raise SystemExit('quarantine observation device subprocess must execute exactly once')
          observation_artifact_name = (
              'name: phone-filesystem-quarantine-observation-${{ env.CANONICAL_SHA }}-${{ github.run_id }}'
          )
          if observe.count(observation_artifact_name) != 3:
              raise SystemExit('quarantine observation must have exactly three bounded persistence attempts')

          cleanup_retry_contract = (
              "id: evidence-upload-1",
              "id: evidence-upload-2",
              "id: evidence-upload-3",
              "steps.evidence-upload-1.outcome != 'success'",
              "steps.evidence-upload-2.outcome != 'success'",
              "EVIDENCE_UPLOAD_1_OUTCOME: ${{ steps.evidence-upload-1.outcome }}",
              "EVIDENCE_UPLOAD_2_OUTCOME: ${{ steps.evidence-upload-2.outcome }}",
              "EVIDENCE_UPLOAD_3_OUTCOME: ${{ steps.evidence-upload-3.outcome }}",
          )
          missing_cleanup_retry = [item for item in cleanup_retry_contract if item not in cleanup]
          if missing_cleanup_retry:
              raise SystemExit(
                  'quarantine cleanup persistence retry contract missing: '
                  + ', '.join(missing_cleanup_retry)
              )
          if cleanup.count("'--mode', 'cleanup'") != 1:
              raise SystemExit('quarantine cleanup device operation must execute exactly once')
          if cleanup.count('subprocess.run(command, check=True)') != 1:
              raise SystemExit('quarantine cleanup device subprocess must execute exactly once')
          cleanup_artifact_name = (
              'name: phone-filesystem-quarantine-cleanup-${{ env.CANONICAL_SHA }}-${{ github.run_id }}'
          )
          if cleanup.count(cleanup_artifact_name) != 3:
              raise SystemExit('quarantine cleanup must have exactly three bounded persistence attempts')

          def evidence_persisted(result_validated, upload_outcomes):
              return result_validated and any(outcome == 'success' for outcome in upload_outcomes)

          persistence_truth_table = (
              ((True, ('success', 'skipped', 'skipped')), True),
              ((True, ('failure', 'success', 'skipped')), True),
              ((True, ('failure', 'failure', 'success')), True),
              ((True, ('failure', 'failure', 'failure')), False),
              ((False, ('success', 'skipped', 'skipped')), False),
          )
          for inputs, expected in persistence_truth_table:
              actual = evidence_persisted(*inputs)
              if actual != expected:
                  raise SystemExit(
                      f'quarantine evidence persistence retry truth table differs: inputs={inputs} expected={expected} actual={actual}'
                  )

          def classify_observation(validated, persisted, complete, all_absent, admissible):
'''
if policy.count(anchor) != 1:
    raise SystemExit('policy retry insertion anchor drifted')
policy = policy.replace(anchor, retry_checks)
policy_path.write_text(policy, encoding='utf-8')
