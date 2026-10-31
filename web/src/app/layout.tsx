import type { Metadata, Viewport } from "next";
import { Red_Hat_Display, Red_Hat_Mono, Red_Hat_Text } from "next/font/google";

import "./globals.css";
import { Providers } from "./providers";

// One superfamily: Display for headings and figures, Text for everything else, Mono for
// domains, case ids, file names and hashes. latin-ext covers Polish and Turkish names.
const display = Red_Hat_Display({
  subsets: ["latin", "latin-ext"],
  variable: "--font-red-hat-display",
  display: "swap",
});
const text = Red_Hat_Text({
  subsets: ["latin", "latin-ext"],
  variable: "--font-red-hat-text",
  display: "swap",
});
const mono = Red_Hat_Mono({
  subsets: ["latin", "latin-ext"],
  variable: "--font-red-hat-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: { default: "Kalkan", template: "%s · Kalkan" },
  description: "Brand protection and phishing takedown automation.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f2f4f7" },
    { media: "(prefers-color-scheme: dark)", color: "#0e1319" },
  ],
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${display.variable} ${text.variable} ${mono.variable}`}
    >
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
