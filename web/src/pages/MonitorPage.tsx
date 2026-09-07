// /monitor: 监控 (T1 chrome + /kb, T2/T3 fill it in).
//
// T1 chrome spec: the third chrome entry must render a route at
// /monitor and the chrome label must read "监控". The page itself
// is a placeholder — T2 (watchlist) and T3 (retrieve-down + 重连,
// StatusStrip removed) are responsible for the real surface. Until
// then this is a no-frills notice so the operator does not see an
// empty box when they pick 监控.

export default function MonitorPage() {
  return (
    <main className="p-6">
      <h2 className="text-xl font-bold mb-1">监控</h2>
      <p className="text-sm text-gray-600 mb-4">
        文件监控与检索服务状态。T2 / T3 即将补上。
      </p>
      <div className="border border-dashed border-black bg-card p-4 max-w-md text-sm text-gray-700">
        监控页尚在搭建中 — 暂时只有占位说明，T2 起会显示 watchlist 与
        检索可达性。
      </div>
    </main>
  );
}
