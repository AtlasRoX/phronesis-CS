import type { Metadata, Viewport } from "next";
import { Google_Sans, Google_Sans_Code } from "next/font/google";
import "./globals.css";
import { AgentationProvider } from "@/components/AgentationProvider";

// Self-hosted by next/font (no runtime request to Google, no layout shift).
const googleSans = Google_Sans({
  subsets: ["latin", "latin-ext"],
  weight: "variable",
  style: ["normal", "italic"],
  axes: ["opsz", "GRAD"],
  display: "swap",
  // No metric overrides exist for Google Sans; use an explicit system fallback instead.
  adjustFontFallback: false,
  fallback: ["Segoe UI", "system-ui", "-apple-system", "sans-serif"],
  variable: "--font-google-sans",
});

const googleSansCode = Google_Sans_Code({
  subsets: ["latin"],
  weight: "variable",
  display: "swap",
  adjustFontFallback: false,
  fallback: ["ui-monospace", "Consolas", "monospace"],
  variable: "--font-google-sans-code",
});

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: dark)", color: "#05060f" },
    { media: "(prefers-color-scheme: light)", color: "#fbfbfe" },
  ],
};

export const metadata: Metadata = {
  title: "Phronesis | Clinical R&D Assistant",
  description:
    "Phronesis - Advanced Autonomous Clinical R&D Multi-Agent Intelligence System. Made by ChronoStrider Engineering (ChronoStrider Enginnerings).",
  icons: {
    icon: "/icon-tight.png",
    shortcut: "/icon-tight.png",
    apple: "/icon-tight.png",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning className={`${googleSans.variable} ${googleSansCode.variable}`}>
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: `
              (function() {
                try {
                  var saved = localStorage.getItem('phronesis-theme');
                  var theme = saved || (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
                  document.documentElement.classList.remove('light', 'dark');
                  document.documentElement.classList.add(theme);
                  document.documentElement.setAttribute('data-theme', theme);
                } catch (e) {}
              })();
            `,
          }}
        />
      </head>
      <body suppressHydrationWarning>
        {children}
        {/* Dev-only feedback toolbar: it posts to a local agentation server and must not ship to users. */}
        {process.env.NODE_ENV === "development" && <AgentationProvider />}
      </body>
    </html>
  );
}
