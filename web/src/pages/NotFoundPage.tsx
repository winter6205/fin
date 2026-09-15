// Catch-all for a deep link that is not a product route (e.g. the
// retired ``/kb`` / ``/monitor``). Renders a plain 404 — deliberately
// NOT ``<Navigate to="/">`` and with no link to a dashboard or any
// external UI (spec SC7 / CONTEXT **Corpus 首页**).

export default function NotFoundPage() {
  return (
    <main className="flex items-center justify-center min-h-[60vh] p-8">
      <div className="max-w-lg border border-black bg-card p-6">
        <h2 className="text-lg font-semibold mb-2">页面不存在</h2>
        <p className="text-sm text-gray-600">
          此地址不是工作台的页面。研究 thread 可在侧栏「会话」列表中打开。
        </p>
      </div>
    </main>
  );
}
