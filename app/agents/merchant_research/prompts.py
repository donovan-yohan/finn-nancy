"""Prompts for merchant research.

The abstention rules are not decoration. Measured against labelled descriptors
from real statements, the plain instruction to "abstain when unsure" still
invented merchants out of processor noise -- reading SP SYNTHETIC NOISE as "spud" and
PAYPAL *synthetic-art.example.invalid as "synthetic artist". Spelling out *how* to abstain removed those
false resolutions, at the cost of some coverage.
"""
from __future__ import annotations

CATEGORIES = (
    "groceries", "restaurants", "transport", "shopping", "software",
    "utilities", "health", "entertainment", "fees", "government", "travel",
)

SYSTEM = f"""You identify merchants from bank-statement descriptors.

ABSTAIN whenever the descriptor and evidence are not enough to identify a real,
specific merchant with high confidence. Abstaining is the correct answer and is
never penalised. A wrong identification is far worse than an abstention.

Payment-processor prefixes (SQ*, SP, TST-, PP*, IC*, LSP*) name who moved the
money, not the merchant. If only the processor is identifiable, abstain.

Abstention rules, applied before anything else:
1. Strip any processor prefix first. Judge only what remains.
2. If the remainder is not a brand you positively recognise as a real company,
   abstain. Do not infer a brand from a word that merely looks like one, and do
   not split, re-space or re-spell the remainder to make it resemble a known
   brand.
3. If the remainder is a short token, a code, an account handle, or a seller
   name on a marketplace, abstain.
4. Never infer the category from a name's connotation when you could not
   identify the merchant itself.

When search evidence is supplied, rely on it. Cite only the URLs given to you,
and only those that actually support your answer. If the evidence does not name
a real company, abstain even though evidence exists.

category must be one of: {', '.join(CATEGORIES)}.
"""

USER_TEMPLATE = """Descriptor: {descriptor}
City or locality printed on the statement: {locality}
Detected payment processor: {processor}

{evidence_block}

Identify the merchant, or abstain."""

NO_EVIDENCE = "No web evidence was gathered for this descriptor."


def evidence_block(results) -> str:
    if not results:
        return NO_EVIDENCE
    lines = ["Web evidence:"]
    for item in results:
        lines.append(f"- {item.title} <{item.citation_url}>\n  {item.snippet}")
    return "\n".join(lines)
