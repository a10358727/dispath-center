import type { HTMLAttributes } from "react";
import { cn } from "@/lib";

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("rounded-lg border border-slate-200 bg-white p-4 shadow-sm", className)} {...props} />;
}

export function CardTitle({ className, ...props }: HTMLAttributes<HTMLHeadingElement>) {
  return <h3 className={cn("mb-2 text-sm font-semibold text-slate-900", className)} {...props} />;
}
