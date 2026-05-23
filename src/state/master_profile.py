"""Master profile loader, validator, and DB rebuild logic.

Iteration 0: MasterProfile stub established at this location per project
decision. Full schema and rebuild pipeline land in Iteration 2.

Lifecycle (architecture doc Section 3.2):
    1. Detect master_profile.yaml mtime change vs master_meta table.
    2. Validate YAML against MasterProfile schema (this file). Fail loudly
       + Telegram alert on any schema violation.
    3. Generate canonical master_profile.json.
    4. Diff against DB:
         new bullets/summaries/aliases → embed + insert (is_active=True)
         changed → re-embed + update
         removed → is_active=False, deactivated_at=now  (NEVER hard-delete)
    5. Update master_meta.master_profile_processed_at.
"""

from pydantic import BaseModel


class MasterProfile(BaseModel):
    """Stub. Full schema lands in Iteration 2.

    Will validate the complete master_profile.yaml structure: personal,
    summaries (with role_categories), work_experience (with
    safe_title_aliases allow-lists and bullet_pools), projects (with
    link + bullet_pool), skills_pool (flat list — categorisation is
    per-JD at build time, not stored here), education, certifications.
    """
