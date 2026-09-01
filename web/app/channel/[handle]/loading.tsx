export default function Loading() {
  return (
    <div className="layout">
      <div className="back-link" style={{ opacity: 0.3 }}>
        &larr; Back to rankings
      </div>
      <div className="channel-header" style={{ opacity: 0.15 }}>
        <div className="channel-header-left">
          <div className="channel-thumb-large">
            <div style={{ width: "100%", aspectRatio: "1", background: "rgba(255,255,255,0.06)" }} />
          </div>
          <div className="channel-header-info">
            <div style={{ width: 280, height: 40, background: "rgba(255,255,255,0.06)" }} />
            <div style={{ width: 180, height: 16, background: "rgba(255,255,255,0.04)" }} />
            <div style={{ width: 120, height: 16, background: "rgba(255,255,255,0.04)" }} />
          </div>
        </div>
        <div className="channel-header-score" style={{ opacity: 0.3 }}>--</div>
      </div>
    </div>
  );
}
