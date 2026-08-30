import type { ButtonHTMLAttributes } from "react";
import { cn } from "@/lib";

type Variant = "primary" | "secondary" | "danger" | "ghost";

const variants: Record<Variant, string> = {
  primary: "bg-slate-900 text-white hover:bg-slate-700 disabled:bg-slate-400",
  secondary: "bg-white text-slate-900 border border-slate-300 hover:bg-slate-100 disabled:text-slate-400",
  danger: "bg-rose-600 text-white hover:bg-rose-500 disabled:bg-rose-300",
  ghost: "bg-transparent text-slate-700 hover:bg-slate-200 disabled:text-slate-400",
};

export function Button({ className, variant = "secondary", ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type="button"
      className={cn(
        "inline-flex items-center gap-1 rounded-md px-3 py-1.5 text-sm font-medium transition disabled:cursor-not-allowed",
        variants[variant],
        className,
      )}
      {...props}
    />
  );
}
