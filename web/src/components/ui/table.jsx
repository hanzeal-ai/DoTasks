import { cn } from "@/lib/utils";

export function Table({ className, ...props }) {
  return <table data-slot="table" className={cn("w-full caption-bottom text-sm", className)} {...props} />;
}
export function TableHeader({ className, ...props }) {
  return <thead data-slot="table-header" className={cn("[&_tr]:border-b", className)} {...props} />;
}
export function TableBody({ className, ...props }) {
  return <tbody data-slot="table-body" className={cn("[&_tr:last-child]:border-0", className)} {...props} />;
}
export function TableRow({ className, ...props }) {
  return <tr data-slot="table-row" className={cn("border-b transition-colors hover:bg-muted/50", className)} {...props} />;
}
export function TableHead({ className, ...props }) {
  return <th data-slot="table-head" className={cn("h-10 px-2 text-left align-middle font-medium text-muted-foreground", className)} {...props} />;
}
export function TableCell({ className, ...props }) {
  return <td data-slot="table-cell" className={cn("p-2 align-middle", className)} {...props} />;
}
