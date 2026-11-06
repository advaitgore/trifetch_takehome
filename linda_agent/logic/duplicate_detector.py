"""Duplicate and resend detector operating across global queue order."""
from __future__ import annotations

import logging
from typing import Dict, List, Set, Tuple

from linda_agent.models import ExtractedDocument, FaxMetadata

logger = logging.getLogger(__name__)


class DuplicateDetector:
    """Identifies confirmed resends and duplicates using reliable source document IDs

    and contextual evidence, respecting arrival order.
    """

    @staticmethod
    def identify_duplicates(
        metadata_list: List[FaxMetadata],
        extracted_docs: Dict[str, ExtractedDocument],
    ) -> Set[str]:
        """Identifies duplicate fax IDs based on earliest arrival order.
        
        Returns:
            Set[str]: Set of fax_ids that are confirmed duplicate resends.
        """
        # Sort metadata by received_at ASC, then fax_id ASC
        sorted_meta = sorted(
            metadata_list,
            key=lambda m: (m.received_at, m.fax_id),
        )

        seen_doc_ids: Dict[str, str] = {}
        duplicate_fax_ids: Set[str] = set()

        for meta in sorted_meta:
            fax_id = meta.fax_id
            doc = extracted_docs.get(fax_id)
            if not doc:
                continue

            target_doc_id = doc.original_doc_id or doc.source_doc_id

            if target_doc_id:
                if target_doc_id in seen_doc_ids:
                    original_fax_id = seen_doc_ids[target_doc_id]
                    logger.info(
                        f"Fax {fax_id} (received {meta.received_at}) is a confirmed duplicate/resend "
                        f"of original doc {target_doc_id} in fax {original_fax_id}."
                    )
                    duplicate_fax_ids.add(fax_id)
                else:
                    seen_doc_ids[target_doc_id] = fax_id

        return duplicate_fax_ids
