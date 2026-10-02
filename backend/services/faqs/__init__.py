"""FAQs — tenant-written question/answer lists an agent or a task answers from."""

from __future__ import annotations

from .models import (  # noqa: F401
    MAX_ENTRIES,
    CreateFaqEntriesRequest,
    FaqDefinition,
    FaqDetail,
    FaqEntriesResponse,
    FaqEntry,
    FaqEntryInput,
    FaqForPrompt,
    FaqSummary,
    InlineFaq,
    UpdateFaqEntryRequest,
    UpdateFaqRequest,
)
from .service import (  # noqa: F401
    FaqRef,
    create_faq,
    create_faq_entries,
    delete_faq,
    delete_faq_entry,
    get_faq,
    insert_faq,
    list_faq_refs,
    list_faqs,
    load_stored_faqs,
    update_faq,
    update_faq_entry,
)
