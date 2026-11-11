import type { Metadata } from "next";
import "./globals.css";
import { Shell } from "@/components/Shell";

export const metadata: Metadata = { title: "Nous Drive", description: "Live index of Google Drive and local folders" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen font-sans text-[15px]">
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
