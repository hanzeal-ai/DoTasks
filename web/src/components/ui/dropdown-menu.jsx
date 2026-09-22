import * as DropdownMenuPrimitive from "@radix-ui/react-dropdown-menu";
import { cn } from "@/lib/utils";

export const DropdownMenu = DropdownMenuPrimitive.Root;
export const DropdownMenuTrigger = DropdownMenuPrimitive.Trigger;

export function DropdownMenuContent({ className, sideOffset = 4, ...props }) {
  return <DropdownMenuPrimitive.Portal>
    <DropdownMenuPrimitive.Content data-slot="dropdown-menu-content" sideOffset={sideOffset}
      className={cn("z-50 min-w-40 overflow-hidden rounded-lg border bg-popover p-1 text-popover-foreground shadow-md outline-none", className)} {...props} />
  </DropdownMenuPrimitive.Portal>;
}

export function DropdownMenuItem({ className, ...props }) {
  return <DropdownMenuPrimitive.Item data-slot="dropdown-menu-item"
    className={cn("relative flex cursor-default select-none items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-none focus:bg-accent focus:text-accent-foreground data-[disabled]:pointer-events-none data-[disabled]:opacity-50", className)} {...props} />;
}

export function DropdownMenuSeparator(props) {
  return <DropdownMenuPrimitive.Separator className="-mx-1 my-1 h-px bg-border" {...props} />;
}
