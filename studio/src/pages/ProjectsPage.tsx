import { Link } from "react-router-dom";
import { Card, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { useProjects } from "@/api/hooks";
import { shortCommit } from "@/lib";

export function ProjectsPage() {
  const projects = useProjects();
  return (
    <div className="p-6">
      <div className="mb-4 flex items-center gap-3">
        <h1 className="text-lg font-semibold">專案</h1>
        <Link to="/projects/import" className="text-sm text-sky-700 underline">匯入專案 →</Link>
      </div>
      {projects.isLoading ? <div className="text-sm text-slate-400">載入中…</div> : null}
      {projects.error ? <div className="text-sm text-rose-700">{(projects.error as Error).message}</div> : null}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
        {(projects.data ?? []).map((project) => (
          <Link key={project.name} to={`/projects/${encodeURIComponent(project.name)}`}>
            <Card className="h-full hover:border-slate-400">
              <CardTitle className="flex items-center justify-between">
                {project.name}
                <Link
                  to={`/runs?project=${encodeURIComponent(project.name)}`}
                  className="text-xs font-normal text-sky-700 underline"
                  onClick={(event) => event.stopPropagation()}
                >
                  實驗與 Run →
                </Link>
              </CardTitle>
              <div className="mb-2 truncate text-xs text-slate-500">{project.repo_or_path}</div>
              <div className="flex flex-wrap gap-1">
                {(project.instances ?? []).map((instance, index) => (
                  <Badge key={index} tone={instance.dirty ? "warn" : "neutral"}>
                    {instance.server ?? "?"} {instance.git_branch ?? ""}@{shortCommit(instance.git_commit)}
                  </Badge>
                ))}
              </div>
            </Card>
          </Link>
        ))}
      </div>
      {!projects.isLoading && (projects.data ?? []).length === 0 ? (
        <div className="text-sm text-slate-400">
          還沒有專案；<Link to="/projects/import" className="text-sky-700 underline">匯入專案 →</Link>
        </div>
      ) : null}
    </div>
  );
}
