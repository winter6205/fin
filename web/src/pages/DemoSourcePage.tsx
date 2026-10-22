import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { getDemoSource, type DemoSource, type DemoSourceLocator } from "../demoMode";
import { useDemoPresentation } from "../components/DemoModeSurface";

type DemoSourceViewState =
  | { kind: "loading" }
  | { kind: "loaded"; source: DemoSource }
  | { kind: "unavailable" };

function isSourceDocumentId(value: string | null): value is string {
  return value !== null && value.length > 0 && value.length <= 200;
}

function isSourceVersion(value: string | null): value is string {
  return value !== null && /^[0-9a-f]{64}$/.test(value);
}

function boundedPositiveInteger(value: string | null): number | null {
  if (value === null) return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 && parsed <= 100
    ? parsed
    : null;
}

function locatorFromSearch(search: string): DemoSourceLocator | null {
  const params = new URLSearchParams(search);
  const documentId = params.get("document_id");
  const version = params.get("version");
  const unit = boundedPositiveInteger(params.get("unit"));
  const page = boundedPositiveInteger(params.get("page"));
  if (
    !isSourceDocumentId(documentId) ||
    !isSourceVersion(version) ||
    unit === null ||
    page === null
  ) {
    return null;
  }
  return { document_id: documentId, version, unit, page };
}

function useDemoSourceView(
  search: string,
  enabled: boolean,
): DemoSourceViewState {
  const [state, setState] = useState<DemoSourceViewState>({ kind: "loading" });

  useEffect(() => {
    let active = true;
    const locator = enabled ? locatorFromSearch(search) : null;
    if (!locator) {
      setState({ kind: "unavailable" });
      return () => {
        active = false;
      };
    }
    setState({ kind: "loading" });
    void getDemoSource(locator)
      .then((source) => {
        if (active) setState({ kind: "loaded", source });
      })
      .catch(() => {
        // EXIT: stale, missing, and unavailable demo locators show no source text.
        if (active) setState({ kind: "unavailable" });
      });
    return () => {
      active = false;
    };
  }, [enabled, search]);

  return state;
}

export function DemoSourceViewer({ source }: { source: DemoSource }) {
  return (
    <main
      data-testid="demo-source-viewer"
      className="mx-auto max-w-4xl p-6"
    >
      <h1 data-testid="demo-source-title" className="text-xl font-semibold">
        {source.title}
      </h1>
      <div className="mt-2 flex gap-4 text-sm text-gray-600">
        <p data-testid="demo-source-section">{source.section}</p>
        <p data-testid="demo-source-page">Page {source.page}</p>
      </div>
      <pre
        data-testid="demo-source-content"
        className="mt-4 whitespace-pre-wrap break-words border border-black bg-card p-4 font-mono text-sm"
      >
        {source.content}
      </pre>
    </main>
  );
}

export default function DemoSourcePage() {
  const { enabled } = useDemoPresentation();
  const { search } = useLocation();
  const state = useDemoSourceView(search, enabled);

  if (state.kind === "loaded") return <DemoSourceViewer source={state.source} />;
  if (!enabled || state.kind === "unavailable") {
    return (
      <main data-testid="demo-source-unavailable" className="p-6">
        演示原文暂不可用，请返回对话查看来源列表。
      </main>
    );
  }
  return (
    <main data-testid="demo-source-loading" className="p-6 text-sm text-gray-600">
      正在打开演示原文…
    </main>
  );
}
