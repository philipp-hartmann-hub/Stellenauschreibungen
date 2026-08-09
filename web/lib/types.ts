export type JobRecord = {
  uid: string;
  title: string;
  url: string;
  location: string | null;
  posted_at: string | null;
  deadline: string | null;
  source_id: string;
  source_name: string;
  ebene: string;
  land: string | null;
  adapter: string;
  first_seen: string;
  last_seen: string;
  active: boolean;
};
