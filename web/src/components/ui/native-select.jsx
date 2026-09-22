import { cn } from "@/lib/utils";

export const nativeSelectClasses = "h-9 w-full min-w-0 rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-xs outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50";
export function NativeSelect({ className, ...props }) {
  return <select data-slot="native-select" className={cn(nativeSelectClasses, className)} {...props} />;
}
