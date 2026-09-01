"use client";

import { useState, useRef, useEffect } from "react";
import Link from "next/link";
import { useAuth } from "./AuthProvider";
import { getCachedCredits, setCachedCredits } from "../lib/evalCache";
import { supabase } from "../lib/supabase";

function GoogleIcon({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 48 48" style={{ flexShrink: 0 }}>
      <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/>
      <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/>
      <path fill="#FBBC05" d="M10.53 28.59a14.5 14.5 0 0 1 0-9.18l-7.98-6.19a24.0 24.0 0 0 0 0 21.56l7.98-6.19z"/>
      <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/>
    </svg>
  );
}

export { GoogleIcon };

export default function AuthButton() {
  const { user, loading, signIn, signOut } = useAuth();
  const [open, setOpen] = useState(false);
  const [credits, setCredits] = useState<number | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  // Fetch credits eagerly on mount when signed in
  useEffect(() => {
    if (!user) return;
    // Show cached value instantly
    const cached = getCachedCredits();
    if (cached !== null) {
      setCredits(cached);
    }
    // Always fetch fresh from DB in background
    supabase
      .from("user_eval_credits")
      .select("credits_used, credits_total")
      .eq("user_id", user.id)
      .limit(1)
      .then(({ data }) => {
        if (data && data.length > 0) {
          const remaining = data[0].credits_total - data[0].credits_used;
          setCachedCredits(remaining);
          setCredits(remaining);
        }
      });
  }, [user]);

  if (loading) return null;

  if (user) {
    const fullName =
      user.user_metadata?.full_name ||
      user.user_metadata?.name ||
      user.email?.split("@")[0] ||
      "User";
    const initial = fullName.charAt(0).toUpperCase();
    const rawAvatar = user.user_metadata?.avatar_url;
    const avatar =
      typeof rawAvatar === "string" && rawAvatar.startsWith("https://")
        ? rawAvatar
        : null;

    return (
      <div className="auth-menu" ref={menuRef}>
        {credits !== null && (
          <span className="auth-credits-inline">{credits} credits</span>
        )}
        <button
          className="auth-btn auth-btn-signed-in"
          onClick={() => setOpen(!open)}
        >
          {avatar ? (
            <img
              src={avatar}
              alt=""
              className="auth-avatar"
              referrerPolicy="no-referrer"
            />
          ) : (
            <span className="auth-avatar-initial">{initial}</span>
          )}
          <span className="auth-name">{fullName}</span>
        </button>
        {open && (
          <div className="auth-dropdown">
            {credits !== null && (
              <div className="auth-dropdown-credits">
                {credits} / 30 credits
              </div>
            )}
            <Link
              href="/evaluate"
              className="auth-dropdown-item"
              onClick={() => setOpen(false)}
            >
              My evaluations
            </Link>
            <button
              className="auth-dropdown-item"
              onClick={() => {
                setOpen(false);
                signOut();
              }}
            >
              Sign out
            </button>
          </div>
        )}
      </div>
    );
  }

  return (
    <button className="auth-btn auth-btn-google" onClick={signIn}>
      <GoogleIcon />
      Sign in
    </button>
  );
}
