import { cn } from "@/lib/cn";
import { AppShell } from "./AppShell";
import { Container, Panel, Skeleton, SURFACE } from "./ui";

/** The agent and task editors' shared frame — sticky header, section tabs,
    CoPilot rail, first cards — at the editors' own sizes, so nothing moves when
    the data arrives. Only the agent editor has a cost estimate and a channel. */
export function EditorSkeleton({ agent = false }: { agent?: boolean }) {
  return (
    <AppShell>
      <div className="min-h-screen bg-surface" aria-busy="true" aria-label="Loading">
      <Container className="min-h-screen">
        <div className="-mx-6 border-b border-line px-6">
          <div className="flex min-h-[68px] items-center gap-3 pb-2 pt-3">
            <Skeleton className="h-4 w-[62px]" />
            <span aria-hidden className="h-4 w-px flex-none bg-line-2" />
            <Skeleton className="h-6 w-[220px]" />
            <Skeleton className="h-5 w-[64px] rounded-full" />
            <div className="ml-auto flex items-center gap-2">
              <Skeleton className="h-9 w-[76px] rounded-lg" />
              <Skeleton className="h-9 w-[98px] rounded-lg" />
              <Skeleton className="h-9 w-[76px] rounded-lg" />
            </div>
          </div>
          <div className="flex gap-5 pb-3 pt-1.5">
            {["w-[52px]", "w-[90px]", "w-[56px]", "w-[40px]", "w-[70px]", "w-[64px]", "w-[58px]"].map((w) => (
              <Skeleton key={w} className={cn("h-3.5", w)} />
            ))}
          </div>
        </div>

        <div className="mt-6 grid grid-cols-1 items-start gap-6 xl:grid-cols-[minmax(0,1fr)_clamp(360px,30vw,480px)]">
          <aside className="flex flex-col gap-4 xl:col-start-2 xl:row-start-1 xl:h-[calc(100vh-136px)]">
            {agent && (
              <Panel className="flex flex-col gap-3">
                <Skeleton className="h-4 w-[110px]" />
                <Skeleton className="h-7 w-[140px]" />
                <Skeleton className="h-2 w-full rounded-full" />
              </Panel>
            )}
            <Panel className="flex min-h-0 flex-1 flex-col gap-3 p-4 max-xl:h-[560px] max-xl:flex-none">
              <Skeleton className="h-4 w-[120px]" />
              <div className="flex-1" />
              <Skeleton className="h-11 w-full rounded-lg" />
            </Panel>
          </aside>

          <div className="flex flex-col gap-5 xl:col-start-1 xl:row-start-1">
            {[
              agent && (
                <div key="channel" className="grid gap-2 sm:grid-cols-3">
                  {[0, 1, 2].map((i) => <Skeleton key={i} className="h-[62px] rounded-lg" />)}
                </div>
              ),
              <div key="prompt" className="flex flex-col gap-2">
                <Skeleton className="h-3.5 w-[96px]" />
                <Skeleton className="h-[150px] w-full rounded-lg" />
              </div>,
              <div key="fields" className="flex flex-col gap-3">
                {[0, 1, 2].map((i) => <Skeleton key={i} className="h-10 w-full rounded-lg" />)}
              </div>,
            ].filter(Boolean).map((body, i) => (
              <section key={i} className={cn(SURFACE, "flex flex-col gap-4 px-5 py-4")}>
                <div className="flex flex-col gap-1.5">
                  <Skeleton className="h-4 w-[120px]" />
                  <Skeleton className="h-3 w-[220px]" />
                </div>
                {body}
              </section>
            ))}
          </div>
        </div>
      </Container>
      </div>
    </AppShell>
  );
}
