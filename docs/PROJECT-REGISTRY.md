# Project Registry policy

## Required fields

- `id`: immutable internal ID, independent from display name.
- `repository`: exact `owner/repository` value; no automatic name similarity or fallback.
- `status`: `active`, `setup_required`, `quarantined`, or `disabled`.
- `protected_branches` and `write_branch_prefixes`: branch policy.
- `allowed_paths`: repository-relative path patterns, evaluated after path safety checks.

## Initial mappings

| Project ID | Repository | Status |
|---|---|---|
| `axsikent-it` | `Maktab-PRO/Axsikent-IT` | active |
| `technikum-digital-platform` | `Maktab-PRO/TECHNIKUM-DIGITAL-PLATFORM` | active |
| `telegram-personal-ai` | `Maktab-PRO/telegram-personal-ai` | active |
| `ai-team-control-plane` | not mapped yet | setup_required |
| `unclassified-repository-4` | `Maktab-PRO/aqlli-mahalla-temirqishloq` | quarantined |

The unclassified repository was visible in the connected account's repo list but its project context was not confirmed. This row exists solely to prevent accidental work on it. Do not change it to active until the project identity and desired responsibility are confirmed.

## Change procedure

1. Inspect the target GitHub repository and confirm its human owner/purpose.
2. Add/update one record in a dedicated control-plane branch.
3. Review the diff to confirm no existing repository is mapped twice.
4. Run `python -m ai_team_control_plane.cli check-registry` and the full test suite.
5. Merge the registry change only after review. Do not update registry live from an unreviewed API request.
