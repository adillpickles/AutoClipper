import "@fontsource-variable/jetbrains-mono";
import "@fontsource-variable/plus-jakarta-sans";
import "@fontsource-variable/space-grotesk";
import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AutoClipper",
  description:
    "Turn long Twitch VODs into ranked, captioned 9:16 clips and 16:9 highlights.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="flex min-h-full flex-col bg-surface-canvas font-sans text-ink-primary">
        {children}
      </body>
    </html>
  );
}
