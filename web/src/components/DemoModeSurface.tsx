import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { getDemoSourceIndex, type DemoSourceSummary } from "../demoMode";

interface DemoPresentation {
  enabled: boolean;
  sources: readonly DemoSourceSummary[];
}

const DemoPresentationContext = createContext<DemoPresentation>({
  enabled: false,
  sources: [],
});

function useSources(
  enabled: boolean,
  providedSources: DemoSourceSummary[] | undefined,
): DemoSourceSummary[] {
  const [loadedSources, setLoadedSources] = useState<DemoSourceSummary[]>(
    providedSources ?? [],
  );

  useEffect(() => {
    if (!enabled || providedSources) {
      setLoadedSources(providedSources ?? []);
      return;
    }
    let active = true;
    void getDemoSourceIndex()
      .then((sources) => {
        if (active) setLoadedSources(sources);
      })
      .catch(() => {
        // EXIT: without the fixture index, citations stay plain text.
        if (active) setLoadedSources([]);
      });
    return () => {
      active = false;
    };
  }, [enabled, providedSources]);

  return enabled ? providedSources ?? loadedSources : [];
}

/** Shared route chrome keeps demo context visible after navigation. */
export default function DemoModeSurface({
  enabled,
  children,
  sourceIndex,
}: {
  enabled: boolean;
  children?: ReactNode;
  sourceIndex?: DemoSourceSummary[];
}) {
  const sources = useSources(enabled, sourceIndex);
  return (
    <DemoPresentationContext.Provider value={{ enabled, sources }}>
      {enabled ? (
        <div className="px-6 pt-3">
          <span
            data-testid="demo-mode-badge"
            className="inline-flex border border-black bg-accent px-2 py-1 text-xs font-semibold text-white"
          >
            演示模式
          </span>
        </div>
      ) : null}
      {children}
    </DemoPresentationContext.Provider>
  );
}

export function useDemoPresentation(): DemoPresentation {
  return useContext(DemoPresentationContext);
}
