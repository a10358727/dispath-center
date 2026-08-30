import type { HTMLAttributes } from "react";
import { cn } from "@/lib";

const tones: Record<string, string> = {
  neutral: "bg-slate-200 text-slate-700",
  ok: "bg-emerald-100 text-emerald-800",
  warn: "bg-amber-100 text-amber-800",
  bad: "bg-rose-100 text-rose-800",
  info: "bg-sky-100 text-sky-800",
};

export function Badge({ tone = "neutral", className, ...props }: HTMLAttributes<HTMLSpanElement> & { tone?: keyof typeof tones }) {
  return <span className={cn("inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium", tones[tone], className)} {...props} />;
}

export function stateTone(state: string | null | undefined): keyof typeof tones {
  switch (state) {
    case "working":
    case "submitted":
      return "info";
    case "completed":
    case "ok":
    case "connected":
      return "ok";
    case "input-required":
      return "warn";
    case "failed":
    case "canceled":
      return "bad";
    default:
      return "neutral";
  }
}
