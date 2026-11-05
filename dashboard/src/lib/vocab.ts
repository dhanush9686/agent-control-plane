export const KIND_LABEL: Record<string, string> = {
  cross_merchant: "Another merchant's data exposed",
  unauthorized_refund: "Refund nobody asked for",
  unapproved_execution: "Money moved without approval",
  pii_shown: "Card or ID data shown",
  unsupported_amount: "Amount not in the records",
};
export const CAT_LABEL: Record<string, string> = {
  lookup: "Payment lookup",
  refund: "Refund request",
  cross_merchant: "Other merchant's data",
  injection: "Hidden instruction",
  pii: "Card and ID data",
  arithmetic: "Adding amounts",
};
export const SOURCE_LABEL: Record<string, string> = { "razorpay-test": "Razorpay test mode", synthetic: "Sample data" };
export const catName = (c: string) => CAT_LABEL[c] ?? c.replace(/_/g, " ");
export const kindName = (k: string) => KIND_LABEL[k] ?? k;

export const GUARDRAILS = [
  { key: "scope", title: "Own data only", short: "Another merchant's records look like they do not exist.", kind: "cross_merchant",
    long: "Every tool call is checked against the merchant the session belongs to. A payment that belongs to someone else returns “not found”, exactly as a missing one would, so the agent cannot even confirm it exists." },
  { key: "approval", title: "Human-approved refunds", short: "The AI drafts. A person approves.", kind: "unapproved_execution",
    long: "The agent can only create a refund draft with an idempotency key. Moving money needs a person, and a senior approver above ₹10,000. Over-refunds and refunds with no provenance in the conversation are refused." },
  { key: "mask", title: "Card data masked", short: "Cards, Aadhaar, PAN, UPI and phone numbers.", kind: "pii_shown",
    long: "Answers are scanned for card numbers (Luhn), Aadhaar (Verhoeff), PAN, UPI handles, phone numbers, emails and IFSC codes. Anything found is masked before the user sees it." },
  { key: "numbers", title: "Real numbers only", short: "Every amount must come from the records.", kind: "unsupported_amount",
    long: "Any figure in an answer must come from a tool result or from the user's own message. Otherwise the answer is withheld, so the agent cannot invent an amount." },
  { key: "audit", title: "Tamper-evident log", short: "Each entry fingerprints the one before.", kind: "unauthorized_refund",
    long: "Every request lands in a SHA-256 hash-chained log. Editing an old entry breaks every later link. That shows tampering; it does not prevent it." },
] as const;
