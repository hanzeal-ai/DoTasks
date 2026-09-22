import * as CheckboxPrimitive from "@radix-ui/react-checkbox";
import { cn } from "@/lib/utils";

export function Checkbox({ className, ...props }) {
  return <CheckboxPrimitive.Root data-slot="checkbox" className={cn("size-4 shrink-0 rounded border border-input bg-background text-primary-foreground shadow-xs outline-none focus-visible:ring-3 focus-visible:ring-ring/50 data-[state=checked]:border-primary data-[state=checked]:bg-primary disabled:cursor-not-allowed disabled:opacity-50", className)} {...props}>
    <CheckboxPrimitive.Indicator className="flex items-center justify-center"><svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><path d="m3 8 3 3 7-7" /></svg></CheckboxPrimitive.Indicator>
  </CheckboxPrimitive.Root>;
}
