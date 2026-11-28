import "@fontsource-variable/jetbrains-mono";
import "@fontsource-variable/plus-jakarta-sans";
import "@fontsource-variable/space-grotesk";
import type { Metadata } from "next";

import { SkipLink } from "@/components/ui/skip-link";
import "./globals.css";

export const metadata: Metadata = {
  title: "AutoClipper",
  description:
    "Turn long Twitch VODs into ranked, captioned 9:16 clips and 16:9 highlights.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    // Browser extensions (password managers, Grammarly, dark-mode tools) add attributes to <html>
    // and <body> before React hydrates; that's not an app bug, so only these two elements'
    // own attributes are exempt from the hydration check (#217).
    <html lang="en" className="h-full antialiased" suppressHydrationWarning>
      <body
        className="flex min-h-full flex-col bg-surface-canvas font-sans text-ink-primary"
        suppressHydrationWarning
      >
        <SkipLink />
        {children}
      </body>
    </html>
  );
}
