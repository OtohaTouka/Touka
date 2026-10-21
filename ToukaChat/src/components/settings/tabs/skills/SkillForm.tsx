import { useTranslation } from "react-i18next";
import { Save, X, RefreshCw, PlusCircle } from "lucide-react";
import { Skill, Tool } from "@/lib/types";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

interface Props {
  editingSkill: Skill;
  setEditingSkill: (skill: Skill | null) => void;
  isSavingSkill: boolean;
  onSave: () => Promise<void>;
  systemTools: Tool[];
}

export function SkillForm({ editingSkill, setEditingSkill, isSavingSkill, onSave, systemTools }: Props) {
  const { t } = useTranslation();
  return (
    <div className="space-y-6 rounded-3xl border border-primary/20 bg-muted/30 p-6 duration-200 animate-in zoom-in-95">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="space-y-2">
          <Label className="ml-1 text-xs font-black uppercase tracking-widest text-muted-foreground">{t('settings.skill_form.name_label')}</Label>
          <Input
            value={editingSkill.name}
            onChange={e => setEditingSkill({...editingSkill, name: e.target.value})}
            placeholder={t('settings.skill_form.name_placeholder')}
            className="bg-background border border-border rounded-2xl h-11 text-sm font-bold"
          />
        </div>
        <div className="space-y-2">
          <Label className="ml-1 text-xs font-black uppercase tracking-widest text-muted-foreground">{t('settings.skill_form.id_label')}</Label>
          <Input 
            value={editingSkill.id} 
            onChange={e => setEditingSkill({...editingSkill, id: e.target.value})}
            placeholder={t('settings.skill_form.id_placeholder')}
            className="bg-background border border-border rounded-2xl h-11 text-sm font-mono"
          />
        </div>
      </div>
      
      <div className="space-y-2">
        <Label className="ml-1 text-xs font-black uppercase tracking-widest text-muted-foreground">{t('settings.skill_form.description_label')}</Label>
        <Input 
          value={editingSkill.description} 
          onChange={e => setEditingSkill({...editingSkill, description: e.target.value})}
          placeholder={t('settings.skill_form.description_placeholder')}
          className="bg-background border border-border rounded-2xl h-11 text-sm"
        />
      </div>

      <div className="space-y-2">
        <Label className="ml-1 text-xs font-black uppercase tracking-widest text-muted-foreground">{t('settings.skill_form.system_prompt_label')}</Label>
        <Textarea 
          value={editingSkill.system_prompt} 
          onChange={e => setEditingSkill({...editingSkill, system_prompt: e.target.value})}
          placeholder={t('settings.skill_form.system_prompt_placeholder')}
          rows={6}
          className="bg-background border border-border rounded-2xl text-sm leading-relaxed resize-none"
        />
      </div>

      <div className="space-y-4">
        <Label className="ml-1 text-xs font-black uppercase tracking-widest text-muted-foreground">{t('settings.skill_form.required_tools_label')}</Label>
        <div className="flex min-h-14 flex-wrap items-center gap-2 rounded-2xl border border-border bg-background/50 p-3">
          {editingSkill.required_tools.map(tool => (
            <Badge key={tool} variant="secondary" className="bg-primary/10 text-primary border border-primary/20 rounded-xl px-3 py-1 text-xs font-bold gap-1.5 animate-in zoom-in-90 hover:bg-primary/20 transition-all">
              {tool}
              <button 
                onClick={() => setEditingSkill({...editingSkill, required_tools: editingSkill.required_tools.filter(t => t !== tool)})} 
                className="hover:text-destructive transition-colors"
                aria-label={t('settings.skill_form.remove_tool_aria', { tool })}
              >
                <X size={12}/>
              </button>
            </Badge>
          ))}
          
          <Select 
            onValueChange={(val) => {
              if (val && !editingSkill.required_tools.includes(val)) {
                setEditingSkill({...editingSkill, required_tools: [...editingSkill.required_tools, val]});
              }
            }}
          >
            <SelectTrigger className="w-auto h-8 bg-transparent border-none text-xs text-muted-foreground focus:ring-0 shadow-none hover:text-primary transition-colors gap-2 px-2">
              <PlusCircle className="w-3.5 h-3.5" />
              <SelectValue placeholder={t('settings.skill_form.add_tool_placeholder')} />
            </SelectTrigger>
            <SelectContent>
              {systemTools.length > 0 ? (
                systemTools.map(t => (
                  <SelectItem key={t.name} value={t.name}>{t.name}</SelectItem>
                ))
              ) : (
                <div className="p-2 text-xs italic text-muted-foreground">{t('settings.skill_form.no_tools_available')}</div>
              )}
            </SelectContent>
          </Select>
        </div>
      </div>

      <div className="flex items-center gap-3 pt-4 border-t border-border/50">
        <Button 
          onClick={onSave} 
          disabled={isSavingSkill || !editingSkill.name || !editingSkill.id}
          className="flex-1 h-12 bg-primary-button text-white rounded-2xl text-sm font-bold hover:bg-primary/90 shadow-lg shadow-primary/20 transition-all gap-2"
        >
          {isSavingSkill ? <RefreshCw className="animate-spin w-4 h-4" /> : <Save className="w-4 h-4" />}
          {t('settings.skill_form.save_button')}
        </Button>
        <Button 
          variant="outline"
          onClick={() => setEditingSkill(null)} 
          className="px-6 h-12 border-border text-muted-foreground rounded-2xl text-sm font-bold hover:bg-accent transition-all"
        >
          {t('settings.skill_form.cancel_button')}
        </Button>
      </div>
    </div>
  );
}


