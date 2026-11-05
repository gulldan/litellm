import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders, testQueryClient } from "@/../tests/test-utils";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { getProxyBaseUrl, setGlobalLitellmHeaderName } from "@/components/networking";
import SkillDetail from "./skill_detail";
import type { Plugin, PluginDownloadEvent, PluginDownloadsResponse } from "./types";

const digest = "a".repeat(64);
const archivePath = `/claude-code/plugins/my-skill/archive/${digest}.zip`;
const url = `${getProxyBaseUrl()}${archivePath}`;
const skill: Plugin = {
  id: "private-skill",
  name: "my-skill",
  source: { source: "archive", url, sha256: digest },
  enabled: true,
  version: null,
  description: null,
  created_at: null,
  updated_at: null,
};
const fetchMock = vi.fn<typeof fetch>();
const skillRequestMock = vi.fn<typeof fetch>();
const renderSkill = (source = skill.source, isAdmin = false) =>
  renderWithProviders(
    <SkillDetail skill={{ ...skill, source }} onBack={vi.fn()} accessToken="dashboard-token" isAdmin={isAdmin} />,
  );
const downloadsUrl = `${getProxyBaseUrl()}/claude-code/plugins/my-skill/downloads?limit=25`;
const event = {
  id: "9007199254740993",
  downloaded_at: "2026-10-01T10:00:00Z",
  version: "1.2.0",
  digest,
  authenticated: true,
  user_id: "reader-id",
  user_email: "reader@example.test",
  key_alias: "development",
  team_id: "team-id",
  team_alias: "Engineering",
} satisfies PluginDownloadEvent;
const page = (
  downloads: PluginDownloadEvent[] = [],
  total_downloads = downloads.length,
  next_cursor: string | null = null,
): PluginDownloadsResponse => ({ downloads, total_downloads, next_cursor });

beforeEach(() => {
  testQueryClient.clear();
  skillRequestMock.mockReset().mockRejectedValue(new Error("Unexpected skill request"));
  fetchMock.mockReset().mockImplementation((input, init) => {
    const requestUrl = input instanceof Request ? input.url : String(input);
    if (requestUrl.includes("/.well-known/litellm-ui-config")) {
      return Promise.resolve(Response.json({ claude_code_marketplace_auth_required: true }));
    }
    return skillRequestMock(input, init);
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  setGlobalLitellmHeaderName();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("retries a private ZIP download through the configured auth header and local route", async () => {
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click");
  URL.createObjectURL = vi.fn(() => "blob:skill-download");
  setGlobalLitellmHeaderName("X-LiteLLM-Key");
  renderSkill();
  skillRequestMock.mockResolvedValueOnce(Response.json({ detail: "Archive not found" }, { status: 404 }));
  fireEvent.click(screen.getByRole("button", { name: "Download ZIP" }));
  expect(screen.getByRole("button", { name: "Downloading…" })).toBeDisabled();
  expect(await screen.findByRole("alert")).toHaveTextContent("Archive not found");
  expect(click).not.toHaveBeenCalled();
  skillRequestMock.mockResolvedValueOnce(new Response("zip-bytes", { headers: { "Content-Type": "application/zip" } }));
  fireEvent.click(screen.getByRole("button", { name: "Download ZIP" }));
  await waitFor(() => expect(click).toHaveBeenCalledOnce());
  expect((click.mock.contexts[0] as HTMLAnchorElement).download).toBe("my-skill.zip");
  expect(skillRequestMock).toHaveBeenLastCalledWith(
    url,
    expect.objectContaining({
      method: "GET",
      headers: expect.objectContaining({ "X-LiteLLM-Key": "Bearer dashboard-token" }),
      redirect: "error",
    }),
  );
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("keeps an external archive as an ordinary link even when its path matches the gateway", () => {
  const externalUrl = `https://external.example.com${archivePath}`;
  renderSkill({ ...skill.source, url: externalUrl }, true);
  expect(screen.getByRole("link")).toHaveAttribute("href", externalUrl);
  expect(screen.getByRole("link")).toHaveAttribute("target", "_blank");
  expect(screen.queryByRole("button", { name: "Download ZIP" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "Downloads" }));
  expect(screen.getByText(/Downloads from external sources are not tracked/)).toBeInTheDocument();
  expect(screen.queryByText("Total downloads")).not.toBeInTheDocument();
  expect(skillRequestMock).not.toHaveBeenCalled();
});

it("loads admin history with the configured header and preserves string cursors when loading more", async () => {
  setGlobalLitellmHeaderName("X-LiteLLM-Key");
  renderSkill(skill.source, true);
  const firstPage = Promise.withResolvers<Response>();
  skillRequestMock.mockReturnValueOnce(firstPage.promise);
  fireEvent.click(screen.getByRole("tab", { name: "Downloads" }));
  expect(screen.getByRole("status")).toHaveTextContent("Loading download history");
  expect(screen.getByRole("button", { name: "Refresh" })).toBeDisabled();
  firstPage.resolve(Response.json(page([event], 3, event.id)));
  const reader = await screen.findByRole("row", { name: /reader@example.test/ });
  expect(reader).toHaveTextContent(new Date(event.downloaded_at).toLocaleString("en"));
  expect(reader).toHaveTextContent("development");
  expect(reader).toHaveTextContent("Engineering");
  expect(reader).toHaveTextContent("1.2.0");
  expect(screen.getByRole("columnheader", { name: "User or key owner" })).toBeInTheDocument();
  expect(screen.getByRole("definition")).toHaveTextContent("3");
  expect(skillRequestMock).toHaveBeenLastCalledWith(
    downloadsUrl,
    expect.objectContaining({
      method: "GET",
      headers: expect.objectContaining({ "X-LiteLLM-Key": "Bearer dashboard-token" }),
    }),
  );

  skillRequestMock.mockResolvedValueOnce(
    Response.json(
      page(
        [
          {
            ...event,
            id: "9007199254740992",
            authenticated: false,
            user_id: null,
            user_email: null,
            key_alias: null,
            team_alias: null,
            team_id: null,
            version: null,
          },
          { ...event, id: "9007199254740991", user_id: null, user_email: null },
        ],
        3,
      ),
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "Load more" }));
  expect(await screen.findByRole("cell", { name: "Anonymous" })).toBeInTheDocument();
  expect(screen.getByRole("cell", { name: "Authenticated, no user identity" })).toBeInTheDocument();
  expect(screen.getByRole("cell", { name: "reader@example.test" })).toBeInTheDocument();
  expect(screen.getAllByRole("row")).toHaveLength(4);
  expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  expect(skillRequestMock).toHaveBeenLastCalledWith(`${downloadsUrl}&cursor=${event.id}`, expect.anything());
});

it("keeps history private and copying an install command makes no download request", async () => {
  const user = userEvent.setup();
  renderSkill();
  expect(screen.queryByRole("tab", { name: "Downloads" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("tab", { name: "How to Use" }));
  await user.click(screen.getByRole("button", { name: "Copy", exact: true }));
  expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  expect(skillRequestMock).not.toHaveBeenCalled();
  expect(screen.queryByText("Total downloads")).not.toBeInTheDocument();
});

it("hides loaded admin history immediately when admin access is removed", async () => {
  const view = renderSkill(skill.source, true);
  skillRequestMock.mockResolvedValueOnce(Response.json(page([event])));
  fireEvent.click(screen.getByRole("tab", { name: "Downloads" }));
  expect(await screen.findByRole("cell", { name: event.user_email })).toBeInTheDocument();
  view.rerender(<SkillDetail skill={skill} onBack={vi.fn()} accessToken="dashboard-token" isAdmin={false} />);
  expect(screen.queryByRole("tab", { name: "Downloads" })).not.toBeInTheDocument();
  expect(screen.queryByRole("table", { name: "Download history" })).not.toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
});

it.each([
  {
    change: "skill name",
    nextSkill: {
      ...skill,
      id: "another-skill",
      name: "another-skill",
      source: { ...skill.source, url: url.replace("my-skill", "another-skill") },
    },
    token: "dashboard-token",
  },
  { change: "immutable skill ID", nextSkill: { ...skill, id: "recreated-skill" }, token: "dashboard-token" },
  { change: "access token", nextSkill: skill, token: "another-admin-token" },
])("clears rows and pagination immediately when the $change changes", async ({ nextSkill, token }) => {
  const view = renderSkill(skill.source, true);
  skillRequestMock.mockResolvedValueOnce(Response.json(page([event], 2, event.id)));
  fireEvent.click(screen.getByRole("tab", { name: "Downloads" }));
  expect(await screen.findByRole("cell", { name: event.user_email })).toBeInTheDocument();
  skillRequestMock.mockResolvedValueOnce(
    Response.json(page([{ ...event, id: "9007199254740992", user_email: null }], 2)),
  );
  fireEvent.click(screen.getByRole("button", { name: "Load more" }));
  expect(await screen.findByRole("cell", { name: event.user_id })).toBeInTheDocument();

  const nextPage = Promise.withResolvers<Response>();
  skillRequestMock.mockReturnValueOnce(nextPage.promise);
  view.rerender(<SkillDetail skill={nextSkill} onBack={vi.fn()} accessToken={token} isAdmin />);
  expect(screen.queryByRole("table", { name: "Download history" })).not.toBeInTheDocument();
  expect(screen.queryByText("Total downloads")).not.toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Loading download history");
  expect(skillRequestMock).toHaveBeenLastCalledWith(
    `${getProxyBaseUrl()}/claude-code/plugins/${nextSkill.name}/downloads?limit=25`,
    expect.objectContaining({ headers: expect.objectContaining({ Authorization: `Bearer ${token}` }) }),
  );
  nextPage.resolve(Response.json(page([{ ...event, user_email: "next-reader@example.test" }])));
  expect(await screen.findByRole("cell", { name: "next-reader@example.test" })).toBeInTheDocument();
  expect(screen.queryByRole("cell", { name: event.user_email })).not.toBeInTheDocument();
});

it("shows a history error without inventing a zero count and refreshes to the empty state", async () => {
  renderSkill(skill.source, true);
  skillRequestMock.mockResolvedValueOnce(Response.json({ detail: "Unavailable" }, { status: 500 }));
  fireEvent.click(screen.getByRole("tab", { name: "Downloads" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not load download history");
  expect(screen.queryByText("Total downloads")).not.toBeInTheDocument();
  expect(screen.queryByText("No downloads recorded yet.")).not.toBeInTheDocument();
  skillRequestMock.mockResolvedValueOnce(Response.json(page()));
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  expect(await screen.findByText("No downloads recorded yet.")).toBeInTheDocument();
  expect(screen.getByRole("definition")).toHaveTextContent("0");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("keeps loaded rows after a pagination failure and retries the same cursor", async () => {
  renderSkill(skill.source, true);
  skillRequestMock.mockResolvedValueOnce(Response.json(page([event], 2, event.id)));
  fireEvent.click(screen.getByRole("tab", { name: "Downloads" }));
  expect(await screen.findByRole("cell", { name: event.user_email })).toBeInTheDocument();
  skillRequestMock.mockResolvedValueOnce(Response.json({ detail: "Unavailable" }, { status: 500 }));
  fireEvent.click(screen.getByRole("button", { name: "Load more" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not load download history");
  expect(screen.getByRole("cell", { name: event.user_email })).toBeInTheDocument();
  skillRequestMock.mockResolvedValueOnce(
    Response.json(page([{ ...event, id: "9007199254740992", user_email: null }], 2)),
  );
  fireEvent.click(screen.getByRole("button", { name: "Load more" }));
  expect(await screen.findByRole("cell", { name: event.user_id })).toBeInTheDocument();
  expect(skillRequestMock).toHaveBeenLastCalledWith(`${downloadsUrl}&cursor=${event.id}`, expect.anything());
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("refreshes previously loaded history after a successful ZIP download", async () => {
  URL.createObjectURL = vi.fn(() => "blob:skill-download");
  renderSkill(skill.source, true);
  skillRequestMock.mockResolvedValueOnce(Response.json(page()));
  fireEvent.click(screen.getByRole("tab", { name: "Downloads" }));
  expect(await screen.findByText("No downloads recorded yet.")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "Overview" }));
  skillRequestMock
    .mockResolvedValueOnce(new Response("zip-bytes", { headers: { "Content-Type": "application/zip" } }))
    .mockResolvedValueOnce(Response.json(page([event])));
  fireEvent.click(screen.getByRole("button", { name: "Download ZIP" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Download ZIP" })).toBeEnabled());
  expect(skillRequestMock).toHaveBeenLastCalledWith(downloadsUrl, expect.anything());
  skillRequestMock.mockResolvedValueOnce(Response.json(page([event])));
  fireEvent.click(screen.getByRole("tab", { name: "Downloads" }));
  const history = await screen.findByRole("table", { name: "Download history" });
  expect(within(history).getByRole("cell", { name: event.user_email })).toBeInTheDocument();
  expect(screen.getByRole("definition")).toHaveTextContent("1");
});
