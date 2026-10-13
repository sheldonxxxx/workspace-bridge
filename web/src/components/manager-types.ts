export type ConfirmState = {
  title: string;
  description: string;
  action: () => Promise<void>;
  destructive?: boolean;
};
export type TextDetail = {
  title: string;
  description?: string;
  content: unknown;
};
