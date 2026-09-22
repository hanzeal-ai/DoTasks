import { Button } from "./button";
import { cn } from "@/lib/utils";

export function SidebarMenu({ className, ...props }) {
  return <ul data-slot="sidebar-menu" className={cn("flex w-full min-w-0 flex-col gap-1", className)} {...props} />;
}
export function SidebarMenuItem(props) {
  return <li data-slot="sidebar-menu-item" className="relative" {...props} />;
}
export function SidebarMenuButton({ className, ...props }) {
  return <Button variant="ghost" className={cn("sidebar-item", className)} {...props} />;
}
