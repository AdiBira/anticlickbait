"use client";

import { useAuth } from "./AuthProvider";

export default function HomeSwitch({
  landing,
  dashboard,
}: {
  landing: React.ReactNode;
  dashboard: React.ReactNode;
}) {
  const { user, loading } = useAuth();

  if (loading) return <>{landing}</>;

  return <>{user ? dashboard : landing}</>;
}
