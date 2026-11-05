import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { getClaudeCodePluginsList, deleteClaudeCodePlugin } from "@/components/networking";
import type { Plugin } from "@/components/claude_code_plugins/types";

import ClaudeCodePluginsPanel from "./ClaudeCodePluginsPanel";

vi.mock("@/components/networking", () => ({
  getClaudeCodePluginsList: vi.fn(),
  deleteClaudeCodePlugin: vi.fn(),
}));

vi.mock("./PluginTable", () => ({
  __esModule: true,
  default: ({
    isLoading,
    pluginsList,
    onDeleteClick,
    onPluginClick,
  }: {
    isLoading: boolean;
    pluginsList: Plugin[];
    onDeleteClick: (pluginName: string, displayName: string) => void;
    onPluginClick: (id: string) => void;
  }) => (
    <div data-testid="plugin-table">
      {isLoading ? "table-loading" : "table-loaded"}
      {pluginsList.map((plugin) => (
        <div key={plugin.id}>
          <button onClick={() => onPluginClick(plugin.id)}>Open {plugin.name}</button>
          <button data-testid={`row-delete-${plugin.id}`} onClick={() => onDeleteClick(plugin.name, plugin.name)}>
            row delete
          </button>
        </div>
      ))}
    </div>
  ),
}));

vi.mock("./add_plugin_form", () => ({ __esModule: true, default: () => null }));
vi.mock("@/components/claude_code_plugins/skill_detail", () => ({
  __esModule: true,
  default: ({ skill, isAdmin }: { skill: Plugin; isAdmin?: boolean }) => (
    <section aria-label={`Details for ${skill.name}`}>{isAdmin && <button>Downloads</button>}</section>
  ),
}));

const mockGetClaudeCodePluginsList = vi.mocked(getClaudeCodePluginsList);
const mockDeleteClaudeCodePlugin = vi.mocked(deleteClaudeCodePlugin);

const skill: Plugin = {
  id: "plugin-1",
  name: "my-skill",
  source: { source: "github", repo: "acme/my-skill" },
  enabled: true,
  version: null,
  description: null,
  created_at: null,
  updated_at: null,
};

describe("ClaudeCodePluginsPanel loading state", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("should resolve the loading state when accessToken is null instead of showing the skeleton forever", async () => {
    render(<ClaudeCodePluginsPanel accessToken={null} />);
    expect(await screen.findByText("table-loaded")).toBeInTheDocument();
    expect(mockGetClaudeCodePluginsList).not.toHaveBeenCalled();
  });

  it("should show the loading state until the skills fetch settles", async () => {
    let resolveFetch: (value: { plugins: never[]; count: number }) => void = () => {};
    mockGetClaudeCodePluginsList.mockReturnValue(
      new Promise((resolve) => {
        resolveFetch = resolve;
      }),
    );
    render(<ClaudeCodePluginsPanel accessToken="sk-test" userRole="Admin" />);
    expect(screen.getByText("table-loading")).toBeInTheDocument();

    resolveFetch({ plugins: [], count: 0 });
    expect(await screen.findByText("table-loaded")).toBeInTheDocument();
    expect(mockGetClaudeCodePluginsList).toHaveBeenCalledWith("sk-test", false);
  });
});

describe("ClaudeCodePluginsPanel delete confirmation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetClaudeCodePluginsList.mockResolvedValue({ plugins: [skill], count: 1 });
  });

  it("should ask for confirmation before deleting and name the skill", async () => {
    const user = userEvent.setup();
    render(<ClaudeCodePluginsPanel accessToken="sk-test" userRole="Admin" />);

    await user.click(await screen.findByTestId("row-delete-plugin-1"));

    expect(await screen.findByText(/are you sure you want to delete skill/i)).toBeInTheDocument();
    expect(screen.getByText("my-skill")).toBeInTheDocument();
    expect(screen.getByText("This action cannot be undone.")).toBeInTheDocument();
    expect(mockDeleteClaudeCodePlugin).not.toHaveBeenCalled();
  });

  it("should delete the skill and refresh the list once confirmed", async () => {
    const user = userEvent.setup();
    mockDeleteClaudeCodePlugin.mockResolvedValue({});
    render(<ClaudeCodePluginsPanel accessToken="sk-test" userRole="Admin" />);

    await user.click(await screen.findByTestId("row-delete-plugin-1"));
    await screen.findByText(/are you sure you want to delete skill/i);
    await user.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(mockDeleteClaudeCodePlugin).toHaveBeenCalledWith("sk-test", "my-skill"));
    await waitFor(() => expect(mockGetClaudeCodePluginsList).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText(/are you sure you want to delete skill/i)).not.toBeInTheDocument());
  });

  it("should not delete the skill when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    render(<ClaudeCodePluginsPanel accessToken="sk-test" userRole="Admin" />);

    await user.click(await screen.findByTestId("row-delete-plugin-1"));
    await screen.findByText(/are you sure you want to delete skill/i);
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByText(/are you sure you want to delete skill/i)).not.toBeInTheDocument());
    expect(mockDeleteClaudeCodePlugin).not.toHaveBeenCalled();
  });
});

describe("ClaudeCodePluginsPanel detail access", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetClaudeCodePluginsList.mockResolvedValue({ plugins: [skill], count: 1 });
  });

  it.each([
    ["Admin", true],
    ["proxy_admin", true],
    ["org_admin", false],
    ["Admin Viewer", false],
    ["proxy_admin_viewer", false],
    ["Internal User", false],
    ["internal_user", false],
  ] as const)("limits selected skill download access for %s", async (userRole, canViewDownloads) => {
    render(<ClaudeCodePluginsPanel accessToken="sk-test" userRole={userRole} />);
    fireEvent.click(await screen.findByRole("button", { name: `Open ${skill.name}` }));
    expect(screen.getByRole("region", { name: `Details for ${skill.name}` })).toBeInTheDocument();
    const downloads = screen.queryByRole("button", { name: "Downloads" });
    if (canViewDownloads) {
      expect(downloads).toBeInTheDocument();
      return;
    }
    expect(downloads).not.toBeInTheDocument();
  });
});
