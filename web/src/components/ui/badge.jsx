import { cn } from "@/lib/utils";

export function Badge({ className, ...props }) {
  return <span data-slot="badge" className={cn("inline-flex items-center justify-center rounded-md border border-transparent bg-secondary px-2 py-0.5 text-xs font-medium whitespace-nowrap", className)} {...props} />;
}
