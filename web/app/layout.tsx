import type { Metadata } from "next";
import { Analytics } from "@vercel/analytics/react";
import AuthProvider from "./components/AuthProvider";
import Footer from "./components/Footer";
import "./globals.css";

export const metadata: Metadata = {
  title: "Anticlickbait",
  description: "How honest is YouTube? We scored 150 channels so you don't have to guess.",
  metadataBase: new URL("https://anticlickbait.vercel.app"),
  openGraph: {
    title: "Anticlickbait",
    description: "How honest is YouTube? We scored 150 channels so you don't have to guess.",
    siteName: "Anticlickbait",
  },
  twitter: {
    card: "summary_large_image",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@75..125,100..900&family=Space+Mono:wght@400;700&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>
        <AuthProvider>
          <main style={{ flex: 1 }}>
            {children}
          </main>
          <Analytics />
          <Footer />
        </AuthProvider>
      </body>
    </html>
  );
}
