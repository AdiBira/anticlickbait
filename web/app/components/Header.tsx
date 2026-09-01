"use client";

import Link from "next/link";
import AuthButton from "./AuthButton";

export default function Header({ hideAuth }: { hideAuth?: boolean } = {}) {
  return (
    <header className="site-header">
      <div className="container">
        <div className="header-inner">
          <Link href="/" className="logo">Anticlickbait</Link>
          <nav className="nav-links">
            {!hideAuth && <AuthButton />}
          </nav>
        </div>
      </div>
    </header>
  );
}
