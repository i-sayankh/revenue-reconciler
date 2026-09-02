/**
 * Shared discrepancy-type metadata: display labels and a fixed, stable
 * color per type. The same mapping backs both the dashboard's by-type chart
 * and the drill-down table's badges, so a type always reads as the same
 * color everywhere on the page.
 *
 * Colors are assigned once, in a fixed order, and never re-derived from
 * sort rank or filter state -- a type keeps its color even as counts and
 * filters change. RECONCILED is treated as a status ("this is fine"), not
 * a discrepancy, so it gets the reserved status-good color; the seven real
 * discrepancy types get distinct categorical hues.
 */

export type DiscrepancyType =
  | "AMOUNT_MISMATCH"
  | "CURRENCY_MISMATCH"
  | "DUPLICATE_CHARGE"
  | "MISSING_PAYMENT"
  | "ORPHAN_PAYMENT"
  | "STATUS_CONTRADICTION"
  | "UNSETTLED_PAYMENT"
  | "RECONCILED";

type TypeMeta = {
  label: string;
  /** CSS custom property (defined in globals.css) carrying this type's color. */
  cssVar: string;
};

export const DISCREPANCY_TYPE_META: Record<DiscrepancyType, TypeMeta> = {
  RECONCILED: { label: "Reconciled", cssVar: "--type-reconciled" },
  AMOUNT_MISMATCH: { label: "Amount mismatch", cssVar: "--type-amount-mismatch" },
  CURRENCY_MISMATCH: { label: "Currency mismatch", cssVar: "--type-currency-mismatch" },
  DUPLICATE_CHARGE: { label: "Duplicate charge", cssVar: "--type-duplicate-charge" },
  MISSING_PAYMENT: { label: "Missing payment", cssVar: "--type-missing-payment" },
  ORPHAN_PAYMENT: { label: "Orphan payment", cssVar: "--type-orphan-payment" },
  STATUS_CONTRADICTION: { label: "Status contradiction", cssVar: "--type-status-contradiction" },
  UNSETTLED_PAYMENT: { label: "Unsettled payment", cssVar: "--type-unsettled-payment" },
};

/** Fixed display order: the "good" bucket first, then discrepancy types A-Z. */
export const DISCREPANCY_TYPE_ORDER: DiscrepancyType[] = [
  "RECONCILED",
  "AMOUNT_MISMATCH",
  "CURRENCY_MISMATCH",
  "DUPLICATE_CHARGE",
  "MISSING_PAYMENT",
  "ORPHAN_PAYMENT",
  "STATUS_CONTRADICTION",
  "UNSETTLED_PAYMENT",
];

/** The seven types that represent an actual discrepancy (excludes RECONCILED). */
export const FILTERABLE_DISCREPANCY_TYPES = DISCREPANCY_TYPE_ORDER.filter(
  (type) => type !== "RECONCILED"
);

function isKnownType(type: string): type is DiscrepancyType {
  return type in DISCREPANCY_TYPE_META;
}

export function typeLabel(type: string): string {
  return isKnownType(type) ? DISCREPANCY_TYPE_META[type].label : type;
}

/** A `var(...)` reference resolving to this type's color in the current theme. */
export function typeColorVar(type: string): string {
  return isKnownType(type)
    ? `var(${DISCREPANCY_TYPE_META[type].cssVar})`
    : "var(--muted-foreground)";
}

export function formatCurrency(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);
}

export function formatCount(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}
