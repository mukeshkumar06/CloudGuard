// Shape of the answers the pages read: the Alexa projection (backend cloudguard/deploy/alexa_projection.py), only
// the fields used here. Every field may be missing or null: panels check before showing anything.
import type { Run } from "../ui/data";

export type Page = "overview" | "cost" | "governance" | "assistant" | "tagging" | "activity" | "settings" | "developer";

export interface PageCtx {
  run: Run;
  names: string[]; // tools the server listed for this token
  mode?: string; // fixture | live | undefined (unknown)
  region: string;
  go: (page: Page) => void;
}

export interface Period { label: string; start: string; end_exclusive: string; days: number }
export interface Totals { charges: string; usage: string; fees: string; tax: string; discounts: string; credits: string;
                          refunds: string; calculated_net: string }
export interface Bill { outcome: string; mode: string; speech: string; currency: string; as_of: string; warnings: string[];
                        error_code: string | null; is_final_bill: boolean; period: Period | null; totals: Totals | null;
                        top_services: { service: string; amount: string }[]; service_count: number;
                        previous_month: { previous_charges: string; change: string; change_percent: string | null } | null }
export interface Breakdown { outcome: string; currency: string; error_code: string | null; is_final_bill: boolean;
                             warnings: string[]; period: Period | null; basis: string | null; speech: string;
                             services: { rank: number; service: string; amount: string }[]; total_services: number;
                             excluded_tax: string | null }
export interface Driver { kind: string; name: string; change: string; share_of_gross_increase_percent: string | null }
export interface Comparison { outcome: string; currency: string; error_code: string | null; warnings: string[]; speech: string;
                              earlier: Period | null; later: Period | null; earlier_charges: string | null;
                              later_charges: string | null; charges_change: string | null;
                              charges_change_percent: string | null; net_change: string | null; verdict: string | null;
                              drivers: Driver[]; offsets: Driver[]; limitation: string | null }
export interface Anomalies { outcome: string; currency: string; error_code: string | null; speech: string; status: string | null;
                             window_start: string | null; window_end: string | null; monitor_count: number | null;
                             last_evaluated: string | null; stale: boolean | null; anomaly_count: number;
                             anomalies: { service: string; region: string; start_date: string; end_date: string | null;
                                          expected_spend: string; actual_spend: string; impact: string;
                                          impact_percent: string | null }[] }
export interface Named { display_name: string | null; name_withheld: boolean; service: string; region: string; finding_id: string }
export interface Inventory { outcome: string; speech: string; regions_scanned: string[]; services_scanned: string[];
                             total_resources: number; by_service: Record<string, number>; by_region: Record<string, number>;
                             coverage_complete: boolean; discovery_error_count: number; inaccessible_regions: string[];
                             resources: Named[]; resources_truncated: boolean }
export interface Bucket { total: number; compliant: number; non_compliant: number; unknown: number; compliance_percentage: number | null }
export interface Compliance { outcome: string; speech: string; regions_scanned: string[]; total_discovered: number;
                              compliant: number; non_compliant: number; unknown: number;
                              compliance_percentage: number | null; by_service: Record<string, Bucket>;
                              by_region: Record<string, Bucket>; missing_mandatory_tags: { key: string; count: number }[];
                              invalid_values: { key: string; reason: string; count: number }[];
                              coverage_complete: boolean; discovery_error_count: number; evaluation_error_count: number }
export interface UntaggedRow extends Named { resource_type: string; missing_tags: string[]; invalid_tags: string[] }
export interface Untagged { outcome: string; speech: string; total: number; returned: number; unknown: number;
                            next_cursor: string | null; coverage_complete: boolean; resources: UntaggedRow[] }
export interface Search { outcome: string; speech: string; total: number; returned: number; coverage_complete: boolean;
                          duplicate_count: number; resources: Named[] }
export interface Explanation { display_name: string | null; service: string; region: string; resource_type: string;
                               compliance_status: string; missing_tags: string[]; invalid_tags: string[];
                               evidence_source_api: string; observed_at: string;
                               inspection_error: { kind: string; code: string } | null; explanation: string; speech: string }
export interface Recommendations { display_name: string | null; compliance_status: string; tags_readable: boolean;
                                   verified_tags: string[]; speech: string; next_step: string;
                                   recommendations: { key: string; required: boolean; status: string; reason_code: string;
                                                      confidence: string; recommended_value: string | null;
                                                      value_withheld: boolean; human_input_reason?: string | null }[] }
export interface Preview { status: string; matched: number; changing: number; conflicts: number; overwrite_count: number;
                           regions: string[]; proposed_tag_keys: string[]; resources: Named[]; resources_truncated: boolean;
                           request: unknown | null; request_withheld: boolean; speech: string; next_step: string; mode: string }
export interface Identity { mode: string; synthetic: boolean; account_id_last4: string | null; identity_type: string | null;
                            identity_label: string | null; error: string | null }
