import { Slot } from "@radix-ui/react-slot";
import { cn } from "@/lib/utils";

export function Card({ className, asChild = false, ...props }) {
  const Component = asChild ? Slot : "div";
  return <Component data-slot="card" className={cn("rounded-xl border bg-card text-card-foreground shadow-sm", className)} {...props} />;
}
