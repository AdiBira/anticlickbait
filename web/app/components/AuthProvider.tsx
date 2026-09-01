"use client";

import { createContext, useContext, useEffect, useState } from "react";
import type { User } from "@supabase/supabase-js";
import { supabase } from "../lib/supabase";

interface AuthContextType {
  user: User | null;
  loading: boolean;
  signIn: () => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType>({
  user: null,
  loading: true,
  signIn: async () => {},
  signOut: async () => {},
});

export function useAuth() {
  return useContext(AuthContext);
}

export default function AuthProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((event, session) => {
      setUser(session?.user ?? null);
      setLoading(false);

      // After OAuth sign-in, restore the original URL (with video/channel params)
      if (event === "SIGNED_IN") {
        const returnUrl = sessionStorage.getItem("acb_auth_return");
        if (returnUrl) {
          sessionStorage.removeItem("acb_auth_return");
          const current = window.location.href.split("#")[0];
          const target = returnUrl.split("#")[0];
          if (current !== target) {
            window.location.replace(returnUrl);
          }
        }
      }
    });

    return () => subscription.unsubscribe();
  }, []);

  const signIn = async () => {
    // Save current URL so we can restore it after OAuth redirect
    sessionStorage.setItem("acb_auth_return", window.location.href);
    await supabase.auth.signInWithOAuth({
      provider: "google",
      options: {
        redirectTo: window.location.origin + window.location.pathname,
      },
    });
  };

  const signOut = async () => {
    await supabase.auth.signOut();
  };

  return (
    <AuthContext.Provider value={{ user, loading, signIn, signOut }}>
      {children}
    </AuthContext.Provider>
  );
}
