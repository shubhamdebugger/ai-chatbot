# Founder question — fixed line FX-33 (R07F)

## 1. The problem

Asked about the founder, the AI wrote its own answer:

> *"I don't have verified information about TG Level's founder, so I don't want to guess."*

Founder questions must get one approved answer, never AI-written words (fixed lines rule).

## 2. The change

Same two layers as the guarantee fix (FX-20, see `REDIS_QDRANT_AND_GUARANTEE_FIX.md`):

1. **Gate (no AI call):** a founder word, with **no** plan/offer word and **no** grievance word → `GATE_FIXED`, FX-33, reason `R07F`. The turn costs ₹0.
2. **Decider backup:** for wordings the gate keywords miss, the understand label `asks_founder` → action `ANSWER_FOUNDER`, FX-33 only, before the no-consent row (R08) so it also applies without consent.

| File | Change |
|---|---|
| [content/fixed_lines.json](../content/fixed_lines.json) | New line FX-33 (DRAFT, placeholder text "Founder Details Printed") |
| [tanya/policy.py](../tanya/policy.py) | `FOUNDER_RX` + `OFFER_RX`; in `gate()`, after R07G and before small talk → FX-33, `R07F` |
| [tanya/understand.py](../tanya/understand.py) | New label `asks_founder`; `interest` now also covers "an offer" |
| [tanya/decider.py](../tanya/decider.py) | Row `R07F` after R07G; `ANSWER_FOUNDER` in `FIXED_ONLY` and `NO_EMOJI` |
| [tanya/llm_mock.py](../tanya/llm_mock.py) | `asks_founder` rule; `offer` counts as `interest` |
| [tests/run_tests.py](../tests/run_tests.py) | 4 new tests + founder question in the LangGraph / plain loop check |

Keywords used at the gate:

| Group | Words |
|---|---|
| Founder | founder, founded, owner, owns, malik, CEO, kisne banaya/banayi, kisne shuru, who started, who runs, who made, Tushar, Ghone, sansthapak, संस्थापक, मालिक, तुषार |
| Plan / offer (skip FX-33) | offer, plan, price, kitne, kitna, fees, cost, discount, subscription, ₹ |
| Grievance (skip FX-33) | fraud, refund, cheat, dhokha, complaint |

Why the plan/offer skip: FX-32 tells Day 1–2 users an offer will come from the founder, so "founder ka offer kab aayega?" is a pricing question and keeps the pricing flow (FX-32 / ANSWER_PRICE). A complaint that names the founder still goes to the complaint flow.

## 3. Result

- New tests pass: every founder question (English, Hinglish, Hindi; with and without consent) gives only FX-33 with `llm_calls == []`; first message still shows FX-01 first.
- Rule tests: 78 passed, 2 failed. The failures (`early_trial_pricing_turn_without_consent`, `early_trial_pricing_turn_is_fixed_line_only`) existed before this change: commit `11f9314` changed the FX-32 wording but not the test constants.

## 4. Needs sign-off

1. The real FX-33 wording (currently the placeholder "Founder Details Printed" in all three languages).
2. Adding row R07F and FX-33 to the Spec (Spec → Python → Node).
