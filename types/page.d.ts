// What the page's scripts (leadgen/static/*.js) use that no script declares, and the shapes
// of the server's answers and of the page's own state that their JSDoc types name. The
// type check (tsconfig.json, strict) holds the scripts to these, so a misspelt field or a
// value that may be missing fails there, not in a salesperson's browser.

interface Window {
  /** Point the browser's bar colour at the theme shown ("light", "dark" or "system"). */
  setThemeColor(choice: string): void;
}

/** The page's settings, written into the page (templates/index.html, #page-config). */
interface PageConfig {
  outcomes: string[];
  paused: boolean;
  google_on: boolean;
  yelp_on: boolean;
  flagged: string[];
  admin: string;
  area_miles: number;
}

/** A mark or call that can still be undone: its id, and when that runs out (epoch seconds). */
interface Undo { id: string; until: number }

/** A saved business as GET /leads sends it (web/common.py lead_json). */
interface Lead {
  key: string;
  name: string;
  score: number;
  tier: string;
  tier_label: string;
  lead_type: string;
  flags: string[];
  /** False for competitors and AARCO's own listing: flagged, never asked Yes / No. */
  prospect: boolean;
  closed: boolean;
  category: string;
  address: string;
  city: string;
  zip: string;
  near: string;
  phone: string;
  website: string;
  distance: number | null;
  reasons: string[];
  map_url: string;
  sources: string[];
  /** "yes", "no", or "" when nobody has answered. */
  has_baler: string;
  marked_by: string;
  mark_clicks: number;
  marks_disagreed: boolean;
  last_call_by: string;
  call_outcome: string;
  call_notes: string;
  call_count: number;
  last_call: string;
  last_call_at: number | null;
  earlier_notes: string;
  earlier_notes_when: string;
  /** The phone and who to ask for that the team verified (contacts.py), beside the listing's own. */
  verified_phone: string;
  contact_name: string;
  contact_by: string;
  contact_when: string;
  contact_at: number | null;
  /** How many times it was saved: more than one, and History lists the earlier ones. */
  contact_saves: number;
  undo_mark: Undo | null;
  undo_call: Undo | null;
  /** The page's own: the Yes / No it is saving ("yes" / "no"; "" or absent when none). */
  saving?: string;
}

/** Some fields of a business, to set on every copy the page holds (leads.js updateLead). */
type LeadUpdate = Partial<Lead> & { key: string };

/** How many businesses each tab holds (web/leads.py view_counts), and calls by result. */
interface Counts {
  all: number;
  unchecked: number;
  yes: number;
  no: number;
  competitors: number;
  closed: number;
  called: number;
  outcomes: Record<string, number>;
  [tab: string]: number | Record<string, number>;
}

/** The Calls page's tab counts: every called business the filter keeps, and each result's. */
interface CallCounts { all: number; outcomes: Record<string, number> }

/** GET /leads: one page of a view. */
interface LeadsPage {
  leads: Lead[];
  total: number;
  offset: number;
  counts: Counts;
  recent: Lead[];
  now: number;
  call_counts?: CallCounts;
}

/** GET /leads?since=: what changed (a business that left the view comes as just its key). */
interface LeadChanges {
  changes: true;
  reload?: boolean;
  leads: (LeadUpdate & { in_view?: boolean })[];
  removed: string[];
  total: number;
  counts: Counts;
  recent: Lead[];
  now: number;
  call_counts?: CallCounts;
}

/** A Yes / No click that didn't reach the server: the answer, and why. */
interface FailedMark { value: string; message: string }

/** A call as POST /calls saved it. */
interface SavedCall {
  id: string;
  uid: string;
  at: number;
  when: string;
  outcome: string;
  notes: string;
  by: string;
  undo: Undo;
}

/** GET /calls/<key>: every call to a business, and every Yes / No it was given. */
interface CallHistory {
  calls: { at: number; when: string; outcome: string; notes: string; by: string }[];
  marks: { value: string; when: string; by: string }[];
  /** Every verified contact saved on the business, newest first. */
  contacts: { phone: string; contact: string; by: string; when: string }[];
}

/** A business's verified contact as just saved (POST /contact), in a Lead's own fields. */
type VerifiedContact = Pick<Lead, "verified_phone" | "contact_name" | "contact_by" | "contact_when" | "contact_at" |
  "contact_saves">;

/** Notes typed in the call box and not yet saved, kept per business. */
interface CallDraft { notes: string; outcome: string; id?: string }

/** Where keyboard focus was in the leads list: a business and its control. */
interface FocusSpot { key: string; ctl: string; next?: string[] }

/** Filling in the map areas a search missed (fillin.py), as GET /searches tells it. */
interface Fill {
  state: string;
  left?: number;
  found?: number;
  new?: number;
  where?: string;
  until_text?: string;
  why?: string;
  /** The page's own: the day of the search still filling in, when it isn't today's. */
  from?: string;
}

/** A day's search in the history (daily.py history). */
interface SearchRow {
  day: string;
  when: string;
  at: number;
  location?: string;
  place?: string;
  found_near?: string;
  miles?: number;
  radius?: number;
  leads?: number;
  new?: number;
  failed?: boolean;
  partial?: boolean;
  stopped?: string;
  interrupted?: boolean;
  reason?: string;
  details?: [string, string | number][] | Record<string, string | number>;
  warnings?: string[];
  fill?: Fill;
  free_at?: string;
}

/** An emergency switch (web/finding.py _switch_list). */
interface SiteSwitch { on: boolean; env?: boolean; site?: boolean; when?: string; by?: string; unread?: boolean }

/** The site's problems in the last 7 days (alerts.py recent). */
interface Problems { count: number; latest: { when: string; text: string }[] }

/** The Yelp lookups left (web/common.py yelp_quota). */
interface YelpQuota { text: string; paused?: boolean }

/** GET /searches. */
interface SearchesAnswer {
  searches: SearchRow[];
  current: SearchRow | null;
  today: string;
  used_today: boolean;
  reruns_left?: number;
  filling?: { day: string; when: string; fill: Fill } | null;
  running: string | null;
  cut_off: boolean;
  paused: string | null;
  yelp: YelpQuota | null;
  problems: Problems | null;
  problems_unread: boolean;
  switches?: Record<string, SiteSwitch>;
  admin: string;
}

/** GET /status/<job>: a search's progress, and what it found once done. */
interface Job {
  state: string;
  message: string;
  steps: string[];
  stopping: boolean;
  step: number;
  pct: number;
  elapsed: number;
  skipped: number[];
  slow: boolean;
  stopped?: string;
  found: number;
  new_leads: number;
  saved_count: number | null;
  warnings: string[];
  note: string | null;
  location: string;
  yelp: YelpQuota | null;
  saved: boolean;
}

/** GET /place: where the search would run, and how far that is from AARCO's shop. */
interface Place { label: string; miles: number; area_miles: number; outside: boolean; confirm: string }

/** A tier's row on the Stats page (stats.py summarize). */
interface TierRow { tier: string; saved: number; checked: number; yes: number; pct: number | null }

/** GET /stats. */
interface StatsAnswer {
  saved: number;
  checked: number;
  with_equipment: number;
  average_score: number | null;
  average_tier: string;
  left_out: number;
  closed: number;
  by_tier: TierRow[];
  tier_floors: Record<string, number>;
  min_score: number;
}

/** An error from the server (core.js api): its HTTP status (0: no answer) and its body. */
interface ApiError extends Error {
  status: number;
  body?: { error?: string; field?: string; job_id?: string; confirm_far?: boolean; place?: Place };
}

/** One step of the tutorial (tour.js). */
interface TourStep { page: string; title: string; text: string; target?: string; needs?: string }

/** Everything the page holds (core.js S). */
interface PageState {
  /** The Leads page: the rows shown (one tab, filtered and sorted by the server). */
  leads: Lead[];
  total: number;
  counts: Counts | null;
  recent: Lead[];
  recentOpen?: boolean;
  /** The Calls page: the called businesses shown, of calledTotal. */
  called: Lead[];
  calledLoaded: boolean;
  calledError: string;
  calledTotal: number;
  callCounts: CallCounts | null;
  callLimit: number;
  callSeq: number;
  calledLoading: boolean;
  /** Yes / No clicks being saved, and those that didn't reach the server, by business. */
  sending: Map<string, string>;
  failed: Map<string, FailedMark>;
  loaded: boolean;
  loadError: string;
  refreshError: string;
  viewLoading: boolean;
  seq: number;
  leadView: string;
  callView: string;
  callQ: string;
  q: string;
  tier: string;
  phone: boolean;
  sort: string;
  dir: string;
  limit: number;
  /** The Find leads page: the search being followed, and the form's state. */
  job: string | null;
  error: string;
  locating?: boolean;
  place?: Place;
  switches?: Record<string, SiteSwitch>;
  fillTimer?: number;
  fillFound?: number;
  /** Seconds the server's clock is ahead of this browser's. */
  skew: number;
  busy: number;
  since: number;
  /** Rows just marked that stay put, until this time (ms). */
  pinned: Map<string, number>;
  shiftedAt: number;
  cutOffTimer: number;
}
