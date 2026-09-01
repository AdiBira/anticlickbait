export default function Loading() {
  return (
    <div className="layout" style={{ minHeight: "60vh", display: "flex", alignItems: "center", justifyContent: "center" }}>
      <span style={{
        fontFamily: "var(--font-data)",
        fontSize: "0.8rem",
        textTransform: "uppercase",
        letterSpacing: "0.1em",
        opacity: 0.5,
      }}>
        Loading...
      </span>
    </div>
  );
}
