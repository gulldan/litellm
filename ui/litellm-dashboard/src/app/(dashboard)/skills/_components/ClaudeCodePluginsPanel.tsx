import React, { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Trans, useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { getClaudeCodePluginsList, deleteClaudeCodePlugin } from "@/components/networking";
import AddPluginForm from "./add_plugin_form";
import PluginTable from "./PluginTable";
import SkillDetail from "@/components/claude_code_plugins/skill_detail";
import { isProxyAdminRole, isProxyAdminTierRole, isViewOnlySessionRole } from "@/utils/roles";
import { toast } from "@/lib/toast";
import { Plugin } from "@/components/claude_code_plugins/types";
import { useUrlTab } from "@/hooks/useUrlTab";
import SkillSubmissions from "./SkillSubmissions";

interface ClaudeCodePluginsPanelProps {
  accessToken: string | null;
  userRole?: string;
  isViewOnly?: boolean;
}

const SKILLS_TABS = ["skills", "submissions"] as const;

const SkillsPanelContent: React.FC<ClaudeCodePluginsPanelProps> = ({
  accessToken,
  userRole = "",
  isViewOnly = false,
}) => {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [tab, setTab] = useUrlTab(SKILLS_TABS, "skills");
  const [isAddModalVisible, setIsAddModalVisible] = useState(false);
  const [editingSkill, setEditingSkill] = useState<Plugin>();
  const [isDeleting, setIsDeleting] = useState(false);
  const [pluginToDelete, setPluginToDelete] = useState<{
    name: string;
    displayName: string;
  } | null>(null);
  const [selectedSkillId, setSelectedSkillId] = useState<string | null>(null);
  const readOnly =
    isViewOnly || isViewOnlySessionRole(userRole) || ["Admin Viewer", "Internal Viewer"].includes(userRole);
  const canWrite = Boolean(accessToken && userRole) && !readOnly;
  const isAdmin = isProxyAdminRole(userRole) && canWrite;
  const adminView = isProxyAdminTierRole(userRole);
  const queryScope = ["skills", accessToken, userRole, readOnly] as const;
  const showForm = Boolean(isAddModalVisible || editingSkill) && canWrite;
  const plugins = useQuery({
    queryKey: [...queryScope, "published"],
    queryFn: () => getClaudeCodePluginsList(accessToken!, false),
    enabled: Boolean(accessToken),
  });
  const pluginsList = plugins.data?.plugins ?? [];
  const selectedSkill = pluginsList.find((plugin) => plugin.id === selectedSkillId);
  const refreshSkills = async () => {
    setSelectedSkillId(null);
    await queryClient.invalidateQueries({ queryKey: queryScope, refetchType: "all" });
  };

  const handleDeleteClick = (pluginName: string, displayName: string) => {
    setPluginToDelete({ name: pluginName, displayName });
  };

  const handleDeleteConfirm = async () => {
    if (!pluginToDelete || !accessToken || !isAdmin) return;

    setIsDeleting(true);
    try {
      await deleteClaudeCodePlugin(accessToken, pluginToDelete.name);
      toast.success(t("skills.panel.toasts.deleted", { name: pluginToDelete.displayName }));
      await refreshSkills();
    } catch (error) {
      console.error("Error deleting skill:", error);
      toast.error(t("skills.panel.toasts.deleteFailed"));
    } finally {
      setIsDeleting(false);
      setPluginToDelete(null);
    }
  };

  return (
    <div className="w-full mx-auto flex-auto overflow-y-auto m-8 p-2">
      {selectedSkill ? (
        <SkillDetail
          skill={selectedSkill}
          onBack={() => setSelectedSkillId(null)}
          isAdmin={isAdmin}
          accessToken={accessToken}
          onPublishClick={refreshSkills}
          onEditClick={canWrite ? () => setEditingSkill(selectedSkill) : undefined}
        />
      ) : (
        <>
          <div className="flex flex-col gap-2 mb-4">
            <h1 className="text-2xl font-bold">{t("skills.panel.title")}</h1>
            <p className="text-sm text-muted-foreground">
              <Trans
                i18nKey="skills.panel.intro"
                components={{
                  endpoint: <code className="bg-muted px-1 rounded-sm">/claude-code/marketplace.json</code>,
                }}
              />
            </p>
            <div className="mt-2 flex gap-2">
              <Button onClick={() => setIsAddModalVisible(true)} disabled={!canWrite}>
                {t("skills.panel.addButton")}
              </Button>
            </div>
          </div>

          <Tabs value={tab} onValueChange={(value) => setTab(value === "submissions" ? "submissions" : "skills")}>
            <TabsList variant="line">
              <TabsTrigger value="skills">{t("skills.panel.tabs.skills")}</TabsTrigger>
              <TabsTrigger value="submissions">
                {t(adminView ? "skills.panel.tabs.submissions" : "skills.panel.tabs.mySubmissions")}
              </TabsTrigger>
            </TabsList>
            <TabsContent value="skills">
              {plugins.isError && (
                <div role="alert" className="my-3 text-sm text-destructive">
                  {t("skills.panel.loadFailed")}
                  <Button variant="link" onClick={() => void plugins.refetch()}>
                    {t("skills.submissions.retry")}
                  </Button>
                </div>
              )}
              <PluginTable
                pluginsList={pluginsList}
                isLoading={plugins.isLoading}
                onDeleteClick={handleDeleteClick}
                isAdmin={isAdmin}
                onPluginClick={setSelectedSkillId}
              />
            </TabsContent>
            <TabsContent value="submissions">
              {accessToken && (
                <SkillSubmissions
                  accessToken={accessToken}
                  queryScope={queryScope}
                  canReview={isAdmin}
                  adminView={adminView}
                  onChanged={refreshSkills}
                />
              )}
            </TabsContent>
          </Tabs>
        </>
      )}

      {showForm && (
        <AddPluginForm
          key={editingSkill ? editingSkill.id : "new"}
          visible
          plugin={editingSkill}
          requiresApproval={!isAdmin}
          onClose={() => {
            setIsAddModalVisible(false);
            setEditingSkill(undefined);
          }}
          accessToken={accessToken}
          onSuccess={() => void refreshSkills()}
        />
      )}

      {pluginToDelete && (
        <AlertDialog
          open
          onOpenChange={(open) => {
            if (!open) setPluginToDelete(null);
          }}
        >
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{t("skills.panel.deleteDialog.title")}</AlertDialogTitle>
              <AlertDialogDescription>
                <Trans
                  i18nKey="skills.panel.deleteDialog.confirm"
                  components={{ name: <strong>{pluginToDelete.displayName}</strong> }}
                />
              </AlertDialogDescription>
              <p className="text-sm text-muted-foreground">{t("skills.panel.deleteDialog.irreversible")}</p>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>{t("skills.panel.deleteDialog.cancel")}</AlertDialogCancel>
              <Button variant="destructive" onClick={handleDeleteConfirm} disabled={isDeleting}>
                {t("skills.panel.deleteDialog.delete")}
              </Button>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      )}
    </div>
  );
};

const ClaudeCodePluginsPanel: React.FC<ClaudeCodePluginsPanelProps> = (props) => (
  <SkillsPanelContent key={`${props.accessToken}:${props.userRole}:${props.isViewOnly}`} {...props} />
);

export default ClaudeCodePluginsPanel;
