import { Badge } from "@/components/ui/badge";
import { typeColorVar, typeLabel } from "@/lib/discrepancy-types";

/**
 * A discrepancy type rendered as an outline badge with a color-coded dot.
 *
 * Identity lives in the dot, not the text: the label stays in the normal
 * badge foreground color, so it's always legible regardless of how much
 * contrast a given type's hue has against the badge background.
 */
export function TypeBadge({ type }: { type: string }) {
  return (
    <Badge variant="outline" className="gap-1.5 font-normal">
      <span
        aria-hidden="true"
        className="h-2 w-2 shrink-0 rounded-full"
        style={{ backgroundColor: typeColorVar(type) }}
      />
      {typeLabel(type)}
    </Badge>
  );
}
