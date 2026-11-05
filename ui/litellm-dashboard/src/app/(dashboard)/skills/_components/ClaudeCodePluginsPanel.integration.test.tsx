import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders, testQueryClient } from "@/../tests/test-utils";
import { setGlobalLitellmHeaderName } from "@/components/networking";
import type { Plugin, SkillSubmission } from "@/components/claude_code_plugins/types";
import { toast } from "@/lib/toast";
import ClaudeCodePluginsPanel from "./ClaudeCodePluginsPanel";

const published: Plugin = {
  id: "published-skill",
  name: "shared-skill",
  source: {
    source: "archive",
    url: "http://localhost:4000/claude-code/plugins/shared-skill/archive/approved.zip",
    sha256: "a".repeat(64),
    ref: "approved",
  },
  version: "2.3.4",
  description: "Approved instructions",
  author: { name: "Maintainer", email: "maintainer@example.org" },
  homepage: "https://docs.example.org/skill",
  keywords: ["sales, finance", "review"],
  category: "Development",
  domain: "Engineering",
  namespace: "workflows",
  installation_preference: "available",
  enabled: true,
  created_at: "2026-10-20T00:00:00Z",
  updated_at: "2026-10-20T00:00:00Z",
};

const pending: SkillSubmission = {
  id: "submission-1",
  plugin_name: published.name,
  operation: "update",
  status: "pending_review",
  proposal: {
    name: published.name,
    source: { source: "github", repo: "example/skills", ref: "proposed-ref" },
    version: null,
    description: "Proposed instructions",
    author: published.author,
    homepage: published.homepage,
    keywords: published.keywords,
    category: published.category,
    domain: published.domain,
    namespace: published.namespace,
    installation_preference: published.installation_preference,
  },
  has_archive: true,
  submitted_by: "member@example.org",
  created_at: "2026-10-21T00:00:00Z",
  base_plugin_id: published.id,
  base_updated_at: published.updated_at,
  reviewed_by: null,
  reviewed_at: null,
  rejection_reason: null,
};

const fetchMock = vi.fn<typeof fetch>();
let catalog: Plugin[];
let submissions: SkillSubmission[];
let writes: { path: string; method: string; body: RequestInit["body"] }[];
let reviewFailure: string | null;
let directPublication: boolean;

const respondToWrite = (path: string, body: RequestInit["body"]) => {
  if (path.endsWith("/approve") || path.endsWith("/reject")) {
    if (reviewFailure) return Response.json({ detail: { error: reviewFailure } }, { status: 409 });
    const approved = path.endsWith("/approve");
    const reason = typeof body === "string" ? JSON.parse(body).reason : null;
    submissions = [{ ...pending, status: approved ? "approved" : "rejected", rejection_reason: reason }];
    if (approved) catalog = [{ ...published, ...pending.proposal, version: "2.3.5" }];
    const result = {
      status: "success",
      action: approved ? "approved" : "rejected",
      submission: submissions[0],
      plugin: approved ? catalog[0] : null,
    };
    return Response.json(result);
  }
  if (directPublication) {
    catalog = [{ ...published, version: "3.0.0", description: "Admin-published instructions" }];
    const result = { status: "success", action: "updated", plugin: catalog[0], submission_id: null };
    return Response.json(result);
  }
  submissions = [pending];
  const result = {
    status: "pending_review",
    action: "submitted",
    submission_id: pending.id,
    plugin: { ...published, id: pending.id, description: "Proposed instructions", enabled: false },
  };
  return Response.json(result);
};

beforeEach(() => {
  testQueryClient.clear();
  vi.clearAllMocks();
  catalog = [published];
  submissions = [pending];
  writes = [];
  reviewFailure = null;
  directPublication = false;
  fetchMock.mockReset().mockImplementation(async (input, options) => {
    const request = input instanceof Request ? input : null;
    const path = new URL(request?.url ?? String(input), "http://localhost").pathname;
    const method = request?.method ?? options?.method ?? "GET";
    if (method !== "GET") {
      const body = request ? await request.text() : options?.body;
      writes.push({ path, method, body });
      return respondToWrite(path, body);
    }
    if (path === "/claude-code/plugins") return Response.json({ plugins: catalog, count: catalog.length });
    if (path === `/claude-code/plugins/${published.name}/versions`)
      return Response.json({
        versions: [
          {
            id: "published-version",
            version: published.version,
            description: "Recorded release",
            manifest: { source: published.source },
            published_at: published.updated_at,
            published_by: "maintainer@example.org",
            content_kind: "stored_archive",
            archive_path: "/claude-code/plugins/shared-skill/versions/published-version/archive",
          },
        ],
        next_offset: null,
      });
    if (path === "/claude-code/skill-submissions") return Response.json({ submissions, count: submissions.length });
    if (path.endsWith("/archive")) return new Response("zip-bytes", { headers: { "Content-Type": "application/zip" } });
    if (path === `/claude-code/skill-submissions/${pending.id}`)
      return Response.json({ submission: submissions[0], current_plugin: catalog[0] });
    if (path === "/.well-known/litellm-ui-config")
      return Response.json({ claude_code_marketplace_auth_required: true });
    return Response.json({ detail: "Unexpected request" }, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  setGlobalLitellmHeaderName();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const openEdit = async () => {
  fireEvent.click(await screen.findByText(published.name));
  fireEvent.click(screen.getByRole("button", { name: "Edit Skill" }));
  return screen.getByRole("dialog");
};

it.each(["url", "upload"])(
  "allows a member %s submission, explains approval and refreshes their pending list",
  async (sourceType) => {
    submissions = [];
    renderWithProviders(<ClaudeCodePluginsPanel accessToken="member-token" userRole="Internal User" />);
    await screen.findByText("Approved instructions");
    fireEvent.click(screen.getByRole("tab", { name: "My submissions" }));
    await screen.findByText("No submissions yet");
    fireEvent.click(screen.getByRole("tab", { name: "Published skills" }));
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: "Published skills" })).toHaveAttribute("aria-selected", "true"),
    );
    fireEvent.click(screen.getByRole("button", { name: "+ Add Skill" }));
    if (sourceType === "upload") {
      fireEvent.click(screen.getByRole("radio", { name: "Upload ZIP" }));
      fireEvent.change(screen.getByLabelText("Skill ZIP file"), {
        target: { files: [new File(["skill"], "new-skill.zip", { type: "application/zip" })] },
      });
    } else {
      fireEvent.change(screen.getByRole("textbox", { name: "Source URL" }), {
        target: { value: "https://github.com/example/new-skill" },
      });
    }
    fireEvent.click(screen.getByRole("button", { name: "Submit for approval" }));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("Submitted for admin approval. The published skill is unchanged"),
    );
    expect(await screen.findByText("Approved instructions")).toBeInTheDocument();
    expect(screen.queryByText("Proposed instructions")).not.toBeInTheDocument();
    expect(writes[0]).toMatchObject({
      path: sourceType === "upload" ? "/claude-code/plugins/upload" : "/claude-code/plugins",
      method: "POST",
    });
    fireEvent.click(screen.getByRole("tab", { name: "My submissions" }));
    expect(await screen.findByText("Pending review")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "View details" }));
    expect(
      await screen.findByText("Awaiting admin approval. The published skill remains unchanged"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve and publish" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
  },
);

it("edits the list snapshot with automatic version intent and preserves the approved skill while review is pending", async () => {
  renderWithProviders(<ClaudeCodePluginsPanel accessToken="member-token" userRole="Internal User" />);
  const form = await openEdit();
  expect(within(form).getByLabelText("Version override (optional)")).toHaveValue("");
  expect(
    within(form).getByText(
      `Current version ${published.version}. Leave blank for the next patch version, or enter a higher version`,
    ),
  ).toBeInTheDocument();
  expect(within(form).getByPlaceholderText("workflows")).toHaveValue(published.namespace);
  expect(within(form).getByPlaceholderText("my-skill")).toBeDisabled();
  fireEvent.change(within(form).getByPlaceholderText("A skill that helps with..."), {
    target: { value: "Proposed instructions" },
  });
  fireEvent.click(within(form).getByRole("button", { name: "Submit for approval" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(writes[0]).toMatchObject({ path: "/claude-code/plugins/shared-skill", method: "PUT" });
  const expectedMetadata = {
    version: null,
    expected_updated_at: published.updated_at,
    source: published.source,
    domain: published.domain,
    namespace: published.namespace,
    homepage: published.homepage,
    keywords: published.keywords,
    installation_preference: published.installation_preference,
    description: "Proposed instructions",
  };
  expect(JSON.parse(String(writes[0].body))).toMatchObject(expectedMetadata);
  expect(await screen.findByText("Approved instructions")).toBeInTheDocument();
  expect(screen.queryByText("Proposed instructions")).not.toBeInTheDocument();
  fireEvent.click(screen.getByText(published.name));
  expect(screen.getByText("Approved instructions")).toBeInTheDocument();
});

it("opens the edit form from published version history without changing the recorded release", async () => {
  renderWithProviders(<ClaudeCodePluginsPanel accessToken="member-token" userRole="Internal User" />);
  fireEvent.click(await screen.findByText(published.name));
  fireEvent.click(screen.getByText("Version history"));
  expect(await screen.findByText("Recorded release")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Download retained ZIP" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Edit Skill" }));
  const form = within(screen.getByRole("dialog"));
  expect(form.getByPlaceholderText("A skill that helps with...")).toHaveValue(published.description);
  expect(form.getByLabelText("Version override (optional)")).toHaveValue("");
  fireEvent.click(form.getByRole("button", { name: "Cancel" }));
  expect(screen.getByText("Recorded release")).toBeInTheDocument();
  expect(writes).toEqual([]);
});

it("sends ZIP replacement through PUT upload with automatic version intent and a concurrency timestamp", async () => {
  renderWithProviders(<ClaudeCodePluginsPanel accessToken="member-token" userRole="Internal User" />);
  const form = await openEdit();
  fireEvent.click(within(form).getByRole("radio", { name: "Upload ZIP" }));
  const file = new File(["new instructions"], "replacement.zip", { type: "application/zip" });
  fireEvent.change(within(form).getByLabelText("Skill ZIP file"), { target: { files: [file] } });
  fireEvent.click(within(form).getByRole("button", { name: "Submit for approval" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(writes[0]).toMatchObject({ path: "/claude-code/plugins/shared-skill/upload", method: "PUT" });
  const body = writes[0].body as FormData;
  expect(body.get("file")).toBe(file);
  const expectedMetadata = {
    version: null,
    expected_updated_at: published.updated_at,
    homepage: published.homepage,
    domain: published.domain,
    namespace: published.namespace,
  };
  expect(JSON.parse(String(body.get("metadata")))).toMatchObject(expectedMetadata);
  expect(JSON.parse(String(body.get("metadata")))).not.toHaveProperty("name");
  expect(screen.getByText("Approved instructions")).toBeInTheDocument();
});

it.each(["url", "upload"])(
  "keeps direct admin %s publication and wires an explicit version override",
  async (sourceType) => {
    directPublication = true;
    renderWithProviders(<ClaudeCodePluginsPanel accessToken="admin-token" userRole="Admin" />);
    const form = await openEdit();
    fireEvent.change(within(form).getByLabelText("Version override (optional)"), { target: { value: "3.0.0" } });
    fireEvent.change(within(form).getByPlaceholderText("A skill that helps with..."), {
      target: { value: "Admin-published instructions" },
    });
    if (sourceType === "upload") {
      fireEvent.click(within(form).getByRole("radio", { name: "Upload ZIP" }));
      fireEvent.change(within(form).getByLabelText("Skill ZIP file"), {
        target: { files: [new File(["skill"], "replacement.zip", { type: "application/zip" })] },
      });
    } else {
      fireEvent.change(within(form).getByRole("textbox", { name: "Source URL" }), {
        target: { value: "https://github.com/example/replacement" },
      });
    }
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("cell", { name: "Admin-published instructions" })).toBeInTheDocument();
    expect(toast.success).toHaveBeenCalledWith("Skill updated successfully");
    expect(screen.queryByText("Submitted for admin approval")).not.toBeInTheDocument();
    const body = writes[0].body;
    const metadata = body instanceof FormData ? body.get("metadata") : body;
    expect(JSON.parse(String(metadata))).toMatchObject({ version: "3.0.0", expected_updated_at: published.updated_at });
    if (sourceType === "url") {
      expect(JSON.parse(String(metadata))).toMatchObject({ source: { source: "github", repo: "example/replacement" } });
    }
  },
);

it("lets an admin inspect full proposed values, download the private ZIP, approve and see refreshed published details", async () => {
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  vi.stubGlobal(
    "URL",
    Object.assign(URL, { createObjectURL: vi.fn(() => "blob:submission"), revokeObjectURL: vi.fn() }),
  );
  setGlobalLitellmHeaderName("X-LiteLLM-Key");
  renderWithProviders(<ClaudeCodePluginsPanel accessToken="admin-token" userRole="Admin" />, {
    searchParams: "tab=submissions",
  });
  fireEvent.click(await screen.findByRole("button", { name: "Review" }));
  expect(await screen.findByText("Currently published")).toBeInTheDocument();
  const dialog = within(screen.getByRole("dialog"));
  expect(dialog.getByText("Approved instructions")).toBeInTheDocument();
  expect(dialog.getByText("Proposed instructions")).toBeInTheDocument();
  expect(dialog.getByText("Next patch version on approval")).toBeInTheDocument();
  expect(dialog.getByText(/proposed-ref/)).toBeInTheDocument();
  expect(dialog.getAllByText(published.homepage!)).toHaveLength(2);
  expect(dialog.getAllByText(published.namespace!)).toHaveLength(2);
  fireEvent.click(dialog.getByRole("button", { name: "Download submitted ZIP" }));
  await waitFor(() => expect(click).toHaveBeenCalledOnce());
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining(`/skill-submissions/${pending.id}/archive`),
    expect.objectContaining({
      headers: expect.objectContaining({ "X-LiteLLM-Key": "Bearer admin-token" }),
      redirect: "error",
    }),
  );
  fireEvent.click(dialog.getByRole("button", { name: "Approve and publish" }));
  expect(await screen.findByText("Approved")).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "Published skills" }));
  await waitFor(() =>
    expect(screen.getByRole("tab", { name: "Published skills" })).toHaveAttribute("aria-selected", "true"),
  );
  expect(await screen.findByRole("cell", { name: "Proposed instructions" })).toBeInTheDocument();
  expect(screen.queryByText("Approved instructions")).not.toBeInTheDocument();
  fireEvent.click(screen.getByText(published.name));
  expect(screen.getByText("Proposed instructions")).toBeInTheDocument();
  fireEvent.click(screen.getByText("Skills"));
  fireEvent.click(screen.getByRole("tab", { name: "Submissions" }));
  fireEvent.click(await screen.findByRole("button", { name: "View details" }));
  const reviewed = within(screen.getByRole("dialog"));
  expect(await reviewed.findByText("Approved")).toBeInTheDocument();
  expect(reviewed.getAllByText("Proposed instructions")).toHaveLength(2);
  expect(reviewed.queryByText("Approved instructions")).not.toBeInTheDocument();
  expect(reviewed.queryByRole("button", { name: "Approve and publish" })).not.toBeInTheDocument();
  expect(reviewed.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
});

it("saves an admin rejection reason for the author and keeps the published metadata", async () => {
  renderWithProviders(<ClaudeCodePluginsPanel accessToken="admin-token" userRole="Admin" />, {
    searchParams: "tab=submissions",
  });
  fireEvent.click(await screen.findByRole("button", { name: "Review" }));
  fireEvent.change(await screen.findByLabelText("Rejection reason (optional)"), {
    target: { value: "Please include setup instructions" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Reject" }));
  expect(await screen.findByText("Rejection reason: Please include setup instructions")).toBeInTheDocument();
  expect(screen.getByText("Rejected")).toBeInTheDocument();
  expect(writes[0]).toMatchObject({
    path: `/claude-code/skill-submissions/${pending.id}/reject`,
    method: "PUT",
    body: JSON.stringify({ reason: "Please include setup instructions" }),
  });
  fireEvent.click(screen.getByRole("tab", { name: "Published skills" }));
  await waitFor(() =>
    expect(screen.getByRole("tab", { name: "Published skills" })).toHaveAttribute("aria-selected", "true"),
  );
  expect(await screen.findByRole("cell", { name: "Approved instructions" })).toBeInTheDocument();
});

it("refreshes detail after a review conflict and removes actions when another reviewer has already acted", async () => {
  renderWithProviders(<ClaudeCodePluginsPanel accessToken="admin-token" userRole="Admin" />, {
    searchParams: "tab=submissions",
  });
  fireEvent.click(await screen.findByRole("button", { name: "Review" }));
  await screen.findByRole("button", { name: "Approve and publish" });
  reviewFailure = "Submission has already been reviewed";
  submissions = [{ ...pending, status: "rejected", rejection_reason: "Already reviewed elsewhere" }];
  fireEvent.click(screen.getByRole("button", { name: "Approve and publish" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(reviewFailure);
  await waitFor(() => expect(screen.queryByRole("button", { name: "Approve and publish" })).not.toBeInTheDocument());
  expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
  expect(
    within(screen.getByRole("dialog")).getByText("Rejection reason: Already reviewed elsewhere"),
  ).toBeInTheDocument();
});

it("keeps an effective Admin viewer read-only while allowing full submission inspection", async () => {
  renderWithProviders(<ClaudeCodePluginsPanel accessToken="viewer-token" userRole="Admin" isViewOnly />);
  expect(screen.getByRole("button", { name: "+ Add Skill" })).toBeDisabled();
  fireEvent.click(await screen.findByText(published.name));
  expect(screen.queryByRole("button", { name: "Edit Skill" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByText("Skills"));
  fireEvent.click(screen.getByRole("tab", { name: "Submissions" }));
  fireEvent.click(await screen.findByRole("button", { name: "View details" }));
  expect(await screen.findByText("Currently published")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Approve and publish" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
  expect(writes).toEqual([]);
});

it.each(["account", "role", "read-only"])(
  "does not show cached submissions after a change of %s when refresh fails",
  async (change) => {
    const { rerender } = renderWithProviders(<ClaudeCodePluginsPanel accessToken="first-token" userRole="Admin" />, {
      searchParams: "tab=submissions",
    });
    expect(await screen.findByText("Proposed instructions")).toBeInTheDocument();
    fetchMock.mockImplementation(async () => Response.json({ detail: "Unavailable" }, { status: 500 }));
    await act(async () =>
      rerender(
        <ClaudeCodePluginsPanel
          accessToken={change === "account" ? "second-token" : "first-token"}
          userRole={change === "read-only" ? "Admin" : "Internal User"}
          isViewOnly={change === "read-only"}
        />,
      ),
    );
    expect(screen.queryByText("Proposed instructions")).not.toBeInTheDocument();
    expect(screen.queryByText("Submitted by member@example.org")).not.toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not refresh submissions");
  },
);

it.each(["role", "read-only"])("does not reuse a published admin snapshot after a change of %s", async (change) => {
  const { rerender } = renderWithProviders(<ClaudeCodePluginsPanel accessToken="same-token" userRole="Admin" />);
  fireEvent.click(await screen.findByText(published.name));
  expect(screen.getByRole("button", { name: "Edit Skill" })).toBeInTheDocument();
  fetchMock.mockImplementation(async () => Response.json({ detail: "Unavailable" }, { status: 500 }));
  await act(async () =>
    rerender(
      <ClaudeCodePluginsPanel
        accessToken="same-token"
        userRole={change === "read-only" ? "Admin" : "Internal User"}
        isViewOnly={change === "read-only"}
      />,
    ),
  );
  expect(screen.queryByText("Approved instructions")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Edit Skill" })).not.toBeInTheDocument();
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not refresh published skills");
});

it("does not reuse admin review metadata when the same account loses its role and detail refresh fails", async () => {
  const { rerender } = renderWithProviders(<ClaudeCodePluginsPanel accessToken="same-token" userRole="Admin" />, {
    searchParams: "tab=submissions",
  });
  fireEvent.click(await screen.findByRole("button", { name: "Review" }));
  expect(await screen.findByText("Currently published")).toBeInTheDocument();
  expect(within(screen.getByRole("dialog")).getByText("Approved instructions")).toBeInTheDocument();
  const fetchStub = fetchMock.getMockImplementation()!;
  fetchMock.mockImplementation(async (input, options) => {
    const path = new URL(input instanceof Request ? input.url : String(input), "http://localhost").pathname;
    if (path === `/claude-code/skill-submissions/${pending.id}`)
      return Response.json({ detail: "Unavailable" }, { status: 500 });
    return fetchStub(input, options);
  });
  await act(async () => rerender(<ClaudeCodePluginsPanel accessToken="same-token" userRole="Internal User" />));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: "View details" }));
  const dialog = within(screen.getByRole("dialog"));
  expect(dialog.queryByText("Approved instructions")).not.toBeInTheDocument();
  expect(dialog.queryByText("Proposed instructions")).not.toBeInTheDocument();
  expect(dialog.queryByRole("button", { name: "Approve and publish" })).not.toBeInTheDocument();
  expect(await dialog.findByRole("alert")).toHaveTextContent("Could not refresh submissions");
});

it("ignores an in-flight admin submission list that resolves after the same account loses its role", async () => {
  const response = Promise.withResolvers<Response>();
  const requested = Promise.withResolvers<void>();
  const fetchStub = fetchMock.getMockImplementation()!;
  fetchMock.mockImplementation(async (input, options) => {
    const path = new URL(input instanceof Request ? input.url : String(input), "http://localhost").pathname;
    if (path === "/claude-code/skill-submissions") {
      requested.resolve();
      return response.promise;
    }
    return fetchStub(input, options);
  });
  const { rerender } = renderWithProviders(<ClaudeCodePluginsPanel accessToken="same-token" userRole="Admin" />, {
    searchParams: "tab=submissions",
  });
  await requested.promise;
  submissions = [];
  fetchMock.mockImplementation(fetchStub);
  await act(async () => rerender(<ClaudeCodePluginsPanel accessToken="same-token" userRole="Internal User" />));
  expect(await screen.findByText("No submissions yet")).toBeInTheDocument();
  await act(async () => {
    response.resolve(Response.json({ submissions: [pending], count: 1 }));
    await response.promise;
  });
  expect(screen.getByText("No submissions yet")).toBeInTheDocument();
  expect(screen.queryByText("Proposed instructions")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Review" })).not.toBeInTheDocument();
});
