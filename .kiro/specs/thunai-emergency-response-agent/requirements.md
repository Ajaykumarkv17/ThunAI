# Requirements Document

## Introduction

ThunAI is a Neighbourhood Emergency Response Agent for the AWS "Agents for Humans" hackathon, entered in the **Good Neighbor Agents** track. ThunAI serves a named community organisation: the Kollidam Ward-7 Neighbourhood Flood Committee — one volunteer coordinator, 14 registered responders, 3 shelters with 120 combined spaces, and roughly 900 residents living along a monitored river reach.

Today, flood response in that ward is fragmented and manual. A rising river is noticed by chance, a warning is relayed by phone tree, resident calls for help arrive across three channels, responder availability lives in one person's head, and shelter capacity is tracked on paper. The coordinator spends the first 90 minutes of every hazard event doing clerical reconciliation instead of making decisions.

ThunAI replaces that clerical layer with a background multi-agent system built on the Strands Agents SDK and deployed on Amazon Bedrock AgentCore. ThunAI is triggered by data, not by a human typing. ThunAI evaluates safety-critical thresholds with deterministic code, uses language models where unstructured understanding and trade-off reasoning are genuinely required, handles routine coordination autonomously, and surfaces to the coordinator only when a real decision exists — with the stakes, the options, and the default action stated in one screen.

All data used in demonstration and testing is synthetic. ThunAI is a coordination aid for a community organisation and is not an official emergency service.

## Glossary

- **ThunAI_Platform**: The complete deployed system, comprising the agent tier, the state tier, the trigger tier, the human surfaces, and the infrastructure application.
- **Sweep_Scheduler**: The recurring scheduled trigger that starts an unattended hazard sweep on a fixed cadence with no human input.
- **Ingestion_Endpoint**: The event-driven entry point that accepts inbound resident messages, sensor readings, and uploaded hazard media, and starts an unattended run.
- **Sensor_Provider**: The interface supplying river level, rainfall rate, and dam release readings, implemented by either a synthetic fixture backend or a live external source.
- **Fixture_Backend**: The synthetic implementation of every external interface, selected by configuration, requiring no third-party credentials.
- **Rule_Engine**: The deterministic, non-model component that evaluates safety-critical hazard thresholds and escalation-mandate rules, implemented as a custom multi-agent graph node.
- **Monitor_Agent**: The agent that interprets hazard readings in context of prior readings and produces a typed hazard assessment.
- **Intake_Agent**: The agent that converts an unstructured resident message into a typed emergency request.
- **Dispatch_Agent**: The agent that selects a responder for an emergency request and produces a typed dispatch decision.
- **Alert_Agent**: The agent that composes channel-specific and language-specific alert content for an incident.
- **Knowledge_Agent**: The agent that answers resident safety questions from a curated knowledge source with citations.
- **Safety_QA_Agent**: The agent that reviews a proposed outbound communication or irreversible action against the published safety policy before release.
- **Incident_Graph**: The deterministic multi-agent graph that orchestrates Rule_Engine, Monitor_Agent, Intake_Agent, Dispatch_Agent, Alert_Agent, and Safety_QA_Agent for one incident.
- **Coordinator_Orchestrator**: The agent that serves the coordinator's ad-hoc requests by calling specialist agents exposed as tools.
- **Escalation_Policy**: The single configuration module holding every threshold, confidence floor, always-ask category, never-ask category, and timeout default action that determines whether ThunAI_Platform acts autonomously or asks a human.
- **Escalation_Record**: The persisted representation of one pending human decision, containing the interrupt identifier, the decision summary, the reason for asking, the stakes, the available options, the default action, and the response deadline.
- **Escalation_Service**: The component that persists an Escalation_Record, delivers a notification out of band, and resumes the paused agent run when a human response arrives.
- **Decision_Inbox**: The Coordinator_Console view listing every open Escalation_Record with one-tap options.
- **Coordinator_Console**: The React web application used by the ward coordinator.
- **Responder_Interface**: The React view used by a registered responder to accept, progress, and complete an assignment.
- **Resident_Status_Page**: The public React view showing current hazard status, shelter availability, and safety guidance.
- **State_Store**: The shared live state repository holding incidents, emergency requests, responders, shelters, Escalation_Records, and processed-event keys.
- **Audit_Ledger**: The append-only record of every tool call, decision, approval, and state transition.
- **Harness**: The set of lifecycle hooks enforcing the approval gate, per-tool call caps, spend caps, and audit capture.
- **Memory_Store**: The persistence layer that carries conversation state and durable community facts across separate runs and separate processes.
- **Observability_Layer**: The tracing, metrics, and cost-reporting capability covering every agent run.
- **Eval_Suite**: The recorded scenario suite and property-based test suite that verify ThunAI_Platform behaviour.
- **Deployment_App**: The single Python AWS CDK application that provisions every cloud resource for ThunAI_Platform.
- **Idempotency_Key**: A caller-supplied identifier that makes a repeated write produce the same end state as a single write.
- **Irreversible_Action**: Any action in the categories evacuation order, mass broadcast notification, responder dispatch to a location with a reported non-ambulatory occupant, shelter closure, or request closure.
- **Request_Lifecycle**: The declared set of emergency request states and the permitted transitions between them.
- **Community_Language_Configuration**: The configured list of languages in which ThunAI_Platform composes resident-facing content, including Tamil and English.
- **Rule_Set_Version**: The identifier of the active deterministic threshold rule set, changing whenever any threshold value changes.

## Requirements

### Requirement 1: Autonomous Triggering Without Human Input

**User Story:** As the ward coordinator, I want ThunAI to start working on its own from data and events, so that hazard detection does not depend on a person noticing something and typing a prompt.

#### Acceptance Criteria

1. THE Sweep_Scheduler SHALL start a hazard sweep run of ThunAI_Platform on a configured recurring schedule with a configured interval between 5 minutes and 24 hours, evaluated against a configured time zone, within 60 seconds of each scheduled time, and with no human input required to start the run.
2. WHEN Ingestion_Endpoint receives an inbound resident message, THE ThunAI_Platform SHALL start an intake run within 10 seconds of receipt.
3. WHEN Ingestion_Endpoint receives a sensor reading payload, THE ThunAI_Platform SHALL start a hazard evaluation run within 10 seconds of receipt.
4. WHEN Ingestion_Endpoint receives an uploaded hazard image of 10 megabytes or less in a supported image format, THE ThunAI_Platform SHALL start an intake run that includes image interpretation within 10 seconds of receipt.
5. WHEN a run starts, THE ThunAI_Platform SHALL write a run record to Audit_Ledger within 5 seconds of the run start containing the run identifier, the trigger type, the trigger source identifier, the start timestamp in coordinated universal time, and the resolved configuration identifiers.
6. IF a triggered run terminates with an error, or has not produced a terminal run record in Audit_Ledger within a configured maximum run duration of 900 seconds or less, THEN THE ThunAI_Platform SHALL write a failure record to Audit_Ledger identifying the run identifier and the failure cause, and SHALL deliver a failure notification to the coordinator through Escalation_Service within 300 seconds of the failure being detected.
7. THE ThunAI_Platform SHALL expose to Coordinator_Console the run identifier, trigger type, trigger source identifier, start timestamp, and terminal status for up to the 20 most recent runs, ordered by start timestamp from most recent to least recent, and SHALL present an explicit empty state when no runs exist.
8. IF a scheduled sweep time is reached while a previous hazard sweep run of ThunAI_Platform has not reached a terminal state, THEN THE Sweep_Scheduler SHALL skip the new run and write a skip record to Audit_Ledger identifying the skipped scheduled time and the identifier of the in-progress run.
9. IF Ingestion_Endpoint receives a payload whose trigger source identifier matches that of a payload already recorded in Audit_Ledger within the preceding 24 hours, THEN THE ThunAI_Platform SHALL not start an additional run and SHALL write a duplicate-suppression record to Audit_Ledger referencing the identifier of the run started for the original payload.
10. IF Ingestion_Endpoint receives a payload that is missing a required field, exceeds 10 megabytes, or is in an unsupported format, THEN THE ThunAI_Platform SHALL reject the payload without starting a run, return a rejection response to the sender indicating which validation check failed, and write a rejection record to Audit_Ledger.

### Requirement 2: Deterministic Evaluation of Safety-Critical Thresholds

**User Story:** As the ward coordinator, I want threshold decisions computed by fixed rules rather than by a language model, so that the point at which the ward is declared at risk is auditable and repeatable.

#### Acceptance Criteria

1. THE Rule_Engine SHALL evaluate river level thresholds expressed in metres, rainfall rate thresholds expressed in millimetres per hour, dam release thresholds expressed in cubic metres per second, and evacuation-mandate criteria using deterministic code that performs zero model invocations.
2. WHEN Incident_Graph invokes the Rule_Engine node, THE Rule_Engine SHALL return a node result within 1 second containing the computed severity band, the triggered rule identifiers, each input reading with its unit and its source timestamp, the availability state of each reading, the count of model invocations performed during the evaluation, the Rule_Set_Version, and the evaluation timestamp.
3. THE Rule_Engine SHALL assign exactly one severity band from the ordered set NORMAL, WATCH, WARNING, EVACUATE to each evaluation, and SHALL assign the highest band triggered by any individual reading when different readings trigger different bands.
4. WHEN two evaluations receive identical readings with identical source timestamps, identical reading availability states, and an identical Rule_Set_Version, THE Rule_Engine SHALL return identical severity bands and an identical set of triggered rule identifiers.
5. WHERE a reading is absent, is non-numeric, falls outside the configured valid range for its unit, or carries a source timestamp older than the configured staleness limit of 15 minutes, THE Rule_Engine SHALL mark that reading unavailable, SHALL compute the severity band from the remaining available readings, and SHALL record the reason for unavailability in the node result.
6. WHEN the Rule_Engine severity band is EVACUATE, THE ThunAI_Platform SHALL withhold every outbound evacuation communication until a human approval response is recorded or until the Escalation_Policy default action for evacuation communication applies.
7. THE Rule_Engine SHALL publish the active rule set to Coordinator_Console and to Resident_Status_Page, including every threshold value, the unit of every threshold value, the staleness limit, and the Rule_Set_Version, and SHALL publish the updated rule set to both surfaces within 10 seconds of a change to the Rule_Set_Version.
8. THE Rule_Engine SHALL return a severity band that is monotonically non-decreasing with respect to an increase in any single hazard reading while every other reading and the Rule_Set_Version are held constant (correctness property).
9. IF every configured reading is unavailable, THEN THE Rule_Engine SHALL return the most recent severity band computed from available readings, SHALL mark the node result as unverified, and SHALL apply no decrease to the severity band of an open incident.
10. IF the active rule set fails to load or its Rule_Set_Version is absent, THEN THE Rule_Engine SHALL return no severity band, SHALL leave the severity band of every open incident unchanged, and SHALL escalate a configuration-fault decision to the coordinator.

### Requirement 3: Hazard Monitoring and Incident Creation

**User Story:** As the ward coordinator, I want ThunAI to distinguish a meaningful change in river conditions from ordinary fluctuation, so that the ward receives incidents that matter and not a stream of noise.

#### Acceptance Criteria

1. WHEN a hazard sweep run starts, THE Monitor_Agent SHALL retrieve the current value of every configured reading type from Sensor_Provider and SHALL retrieve, from Memory_Store, the readings of the most recently completed sweep for the same river reach and the stored 30-day baseline for each configured reading type, allowing at most 3 retrieval attempts per source and at most 10 seconds per attempt.
2. THE Monitor_Agent SHALL produce a typed hazard assessment containing the severity band supplied by Rule_Engine, the rate of change for each reading type expressed in that reading type's unit per hour, an anomaly indicator expressed as the ratio of the current reading to the stored 30-day baseline for the same reading type, a confidence value in the inclusive range 0.0 to 1.0, an action field that includes an explicit ask-human value, and a rationale of one sentence of at most 200 characters written for a non-technical reader.
3. WHEN the Rule_Engine severity band is WATCH or higher, no open incident covers the same river reach, the assessment confidence value is at or above the Escalation_Policy confidence floor, and the assessment action field does not hold the ask-human value, THE Monitor_Agent SHALL create exactly one incident in State_Store using an Idempotency_Key derived from the river reach identifier and the severity band.
4. WHEN the Rule_Engine severity band is WATCH or higher and an open incident already covers the same river reach, THE Monitor_Agent SHALL update that existing incident with the current readings, SHALL set the incident severity band to the higher of the stored severity band and the current severity band, SHALL create no second incident for that river reach, and SHALL leave the incident unchanged where the current readings and severity band are identical to those of the last recorded update (correctness property: idempotent update).
5. WHEN the Rule_Engine severity band is NORMAL, no open incident covers the same river reach, and the anomaly indicator of every available reading type is below the Escalation_Policy anomaly ratio threshold, THE Monitor_Agent SHALL record the sweep outcome in Audit_Ledger and SHALL complete the run without creating an incident, without escalating a decision, and without sending a notification.
6. IF Sensor_Provider returns an error or no value for every configured reading type after the permitted retrieval attempts, THEN THE Monitor_Agent SHALL record a data-outage event in Audit_Ledger, SHALL leave every existing incident and every stored severity band unchanged, and SHALL escalate a data-outage decision to the coordinator stating the affected reading types, the age of the most recent stored reading in minutes, and the Escalation_Policy default action for a data outage.
7. THE Monitor_Agent SHALL record every created incident and every updated incident in Audit_Ledger with the current readings, the prior readings retrieved from Memory_Store, the computed rate of change, the anomaly indicator, the severity band, and the triggered rule identifiers that justified the change.
8. IF Sensor_Provider returns a value for at least one configured reading type and returns an error or no value for at least one other configured reading type, THEN THE Monitor_Agent SHALL mark each unavailable reading type in the typed hazard assessment, SHALL produce the assessment from the available reading types only, and SHALL record the unavailable reading types in Audit_Ledger.
9. IF Memory_Store holds no prior sweep readings or no 30-day baseline for a configured reading type, THEN THE Monitor_Agent SHALL mark the rate of change and the anomaly indicator for that reading type as unavailable in the typed hazard assessment, SHALL produce the assessment from the current readings alone, and SHALL record the absence of prior readings in Audit_Ledger.
10. WHEN the Rule_Engine severity band is NORMAL and an open incident covers the same river reach, THE Monitor_Agent SHALL update that incident with the current readings, SHALL keep the incident open, and SHALL record the severity downgrade in Audit_Ledger without closing the incident.

### Requirement 4: Multi-Agent Incident Pipeline Orchestration

**User Story:** As a judge evaluating technical implementation, I want the agent topology to be justified by the work, so that the multi-agent design reads as necessary rather than decorative.

#### Acceptance Criteria

1. THE Incident_Graph SHALL execute Rule_Engine, Monitor_Agent, Intake_Agent, Dispatch_Agent, Alert_Agent, and Safety_QA_Agent as nodes whose node set, directed edges, and single entry point are declared before a run starts and remain unchanged for the duration of that run.
2. WHEN two runs are given identical incident input and every node returns an identical outcome in both runs, THE Incident_Graph SHALL produce an identical node execution order for both runs.
3. WHEN both the alert-composition branch and the dispatch branch are reachable for the same incident, THE Incident_Graph SHALL start the later branch before the earlier branch reaches a terminal node status, so that the two branches overlap in execution time.
4. THE Incident_Graph SHALL enforce an overall run execution timeout with a default of 600 seconds and a configurable range of 60 to 1800 seconds, a per-node execution timeout with a default of 120 seconds and a configurable range of 10 to 600 seconds, and a maximum node execution count per run with a default of 25 and a configurable range of 6 to 100.
5. WHEN an Incident_Graph run reaches a terminal state, THE ThunAI_Platform SHALL persist to Audit_Ledger, within 5 seconds of that terminal state, the ordered node execution sequence, exactly one status per declared node from the set succeeded, failed, timed_out, skipped, not_executed, and exactly one run outcome from the set complete, partial, halted, failed, and SHALL make the ordered node execution sequence and per-node statuses readable in Coordinator_Console within the same 5 seconds.
6. IF a non-terminal node of Incident_Graph returns a failure or exceeds the per-node execution timeout, THEN THE Incident_Graph SHALL record that node status as failed or timed_out respectively, SHALL continue executing every branch that does not consume that node's output, SHALL record every node that consumes only that node's output with status not_executed, and SHALL set the run outcome to partial.
7. IF a terminal node of Incident_Graph returns a failure or exceeds the per-node execution timeout, THEN THE Incident_Graph SHALL set the run outcome to failed, SHALL retain every node output produced before the failure, and SHALL surface to Coordinator_Console the incident identifier, the failed node identifier, and the failure reason.
8. IF an Incident_Graph run exceeds the overall run execution timeout or reaches the maximum node execution count, THEN THE Incident_Graph SHALL start no further node, SHALL set the run outcome to halted, SHALL persist the halt reason and the last completed node to Audit_Ledger, and SHALL surface the halted run to Coordinator_Console.
9. IF exactly one of the alert-composition branch and the dispatch branch is reachable for an incident, THEN THE Incident_Graph SHALL execute the reachable branch and SHALL record every node of the unreachable branch with status skipped.
10. THE Coordinator_Orchestrator SHALL expose Monitor_Agent, Intake_Agent, Dispatch_Agent, and Knowledge_Agent as callable tools, SHALL route every ad-hoc coordinator request to at least one of those specialist agents, and SHALL hold no tool that creates, modifies, or deletes incident, dispatch, or alert data.
11. IF an ad-hoc coordinator request cannot be routed to any of the exposed specialist agents, THEN THE Coordinator_Orchestrator SHALL return an unroutable-request indication naming the request and SHALL leave incident, dispatch, and alert data unchanged.
12. THE ThunAI_Platform SHALL assign to each agent role an agent identifier that is unique across all agent roles and a system prompt that differs from the system prompt of every other agent role.
13. THE ThunAI_Platform SHALL attach Memory_Store to the orchestrating component only and SHALL construct every member agent of Incident_Graph with no attached session manager.
14. THE ThunAI_Platform SHALL state, in the design document and in the repository README, for each agent role other than the orchestrating component, the reason that role exists and at least one of the following: the number of tools a single-agent alternative would carry, or two or more named prompt responsibilities that would conflict in a single-agent alternative.

### Requirement 5: Resident Request Intake From Unstructured Messages

**User Story:** As a resident in distress, I want to describe my situation in my own words in my own language, so that I do not have to fill in a form while water is entering my house.

#### Acceptance Criteria

1. WHEN an inbound resident message arrives as text or as an uploaded hazard image, THE Intake_Agent SHALL produce exactly one typed emergency request containing the request category, the reported occupant count as a whole number from 0 to 99 or an explicit unknown value where the message states no count, the location reference, a mobility-assistance indicator holding true, false, or unknown, a medical-need indicator holding true, false, or unknown, the detected source language, an urgency band, a confidence value between 0.0 and 1.0 inclusive, an action field that includes an explicit ask-human value, and a one-sentence rationale of at most 200 characters written for a human reader.
2. THE Intake_Agent SHALL assign exactly one request category from the set RESCUE, MEDICAL, SHELTER, SUPPLIES, INFORMATION, OTHER, SHALL assign INFORMATION only where the message requests no physical assistance, and SHALL assign OTHER only where the message matches no other category in the set.
3. THE Intake_Agent SHALL assign exactly one urgency band from the ordered set IMMEDIATE, URGENT, ROUTINE to every typed emergency request, and SHALL assign IMMEDIATE where the mobility-assistance indicator or the medical-need indicator holds true.
4. WHEN the typed emergency request confidence value is at or above the Escalation_Policy confidence floor and the request category is outside the Escalation_Policy always-ask categories, THE Intake_Agent SHALL create the request in State_Store in a dispatch-eligible state using an Idempotency_Key derived from the inbound message identifier and SHALL raise no escalation for that request.
5. IF the typed emergency request confidence value is below the Escalation_Policy confidence floor or the request category is within the Escalation_Policy always-ask categories, THEN THE Intake_Agent SHALL create the request in State_Store in a non-dispatch-eligible state, SHALL escalate a request-triage decision to the coordinator containing the request category, the urgency band, the confidence value, and the reason for asking, and SHALL withhold dispatch eligibility until a coordinator response resolves that escalation.
6. WHERE the inbound message references a location that Intake_Agent resolves to a number of candidate locations other than exactly one, THE Intake_Agent SHALL record every resolved candidate location up to a maximum of 5 ranked candidates, SHALL escalate a location-clarification decision to the coordinator containing the recorded candidates and the resident wording of the location, and SHALL withhold dispatch eligibility for that request until a coordinator response resolves that escalation.
7. WHEN an inbound message requests information only and requests no physical assistance, THE Intake_Agent SHALL route the message to Knowledge_Agent, SHALL create no dispatch-eligible request, and SHALL record the routing in Audit_Ledger.
8. IF the Intake_Agent assigns no request category to the inbound message or detects a source language outside the Community_Language_Configuration, THEN THE Intake_Agent SHALL preserve the inbound message, SHALL create no dispatch-eligible request, and SHALL escalate a manual-triage decision to the coordinator containing the preserved message and the reason for asking.
9. WHEN the same inbound message identifier is processed a second time, THE Intake_Agent SHALL leave State_Store in the state produced by the first processing, SHALL create no additional emergency request, and SHALL raise no additional escalation and no additional Knowledge_Agent routing (correctness property: idempotence).
10. THE Intake_Agent SHALL store the resident-supplied free text separately from the typed emergency request and SHALL exclude the resident name, the resident contact number, and the resident exact street address from every typed emergency request field exposed to Resident_Status_Page.

### Requirement 6: Responder Dispatch Decisions

**User Story:** As the ward coordinator, I want ThunAI to match requests to responders and fill the routine gaps itself, so that my attention goes to the assignments that involve a genuine trade-off.

#### Acceptance Criteria

1. WHEN a request in State_Store holds a dispatch-eligible status, meaning an open request with a category other than INFORMATION and with no active assignment, THE Dispatch_Agent SHALL retrieve every responder within the configured dispatch search radius in kilometres, up to the configured maximum candidate count, with each candidate distance in kilometres, availability status, equipment capability list, and current active assignment count.
2. THE Dispatch_Agent SHALL produce a typed dispatch decision containing the selected responder identifier, up to the configured maximum alternative count of alternative responder identifiers ranked by suitability, the selection rationale as one sentence within the configured rationale character limit, a confidence value between 0.0 and 1.0, and an action field that includes an explicit ask-human value.
3. WHEN the typed dispatch decision confidence value is at or above the Escalation_Policy confidence floor and the request falls outside the Escalation_Policy always-ask categories, THE Dispatch_Agent SHALL assign the selected responder to the request using an Idempotency_Key derived from the request identifier.
4. THE Dispatch_Agent SHALL select only responders whose availability status is AVAILABLE, SHALL transition an assigned responder availability status from AVAILABLE to ASSIGNED in the same state write as the assignment, and SHALL hold at most one active assignment per responder at any time (correctness property: no double-assignment).
5. WHERE a request carries a mobility-assistance indicator or a medical-need indicator, THE Dispatch_Agent SHALL escalate the dispatch decision to the coordinator through Escalation_Service and SHALL withhold the assignment until an approval response arrives or until the Escalation_Policy response deadline for that escalation type passes and its default action applies.
6. IF no responder within the configured dispatch search radius holds availability status AVAILABLE, or no available responder satisfies the request equipment requirement, THEN THE Dispatch_Agent SHALL escalate a no-capacity decision to the coordinator containing the unmet requirement, the search radius used in kilometres, and up to the configured maximum alternative count of nearest partially-capable candidates with their distance in kilometres and unmet capability, and SHALL leave the request in a dispatch-eligible status.
7. WHEN a shelter placement is part of a dispatch decision, THE Dispatch_Agent SHALL decrement the available capacity of the selected shelter by the reported occupant count of the request and SHALL keep the shelter available capacity at or above zero (correctness property: capacity floor).
8. THE Dispatch_Agent SHALL record every assignment, rejection, escalation, and capacity change in Audit_Ledger with the request identifier, the selected responder identifier, the ranked alternatives that were considered, and the event timestamp.
9. WHEN a dispatch decision for the same request identifier is applied a second time, THE Dispatch_Agent SHALL leave State_Store in the state produced by the first application and SHALL create no second assignment and no second capacity decrement (correctness property: idempotence).
10. IF the selected responder availability status is no longer AVAILABLE at the moment the assignment write is applied, THEN THE Dispatch_Agent SHALL reject the assignment, SHALL leave the responder and the request unchanged, and SHALL produce a new dispatch decision from the remaining ranked alternative responders.
11. IF no shelter holds available capacity at or above the reported occupant count of the request, THEN THE Dispatch_Agent SHALL escalate a shelter-capacity decision to the coordinator containing each shelter available capacity and the capacity shortfall, and SHALL apply no capacity decrement.

### Requirement 7: Alert Composition and Multilingual Delivery

**User Story:** As a resident, I want warnings in my own language, on the channel I actually read, phrased so that I know what to do, so that a warning changes my behaviour instead of confusing me.

#### Acceptance Criteria

1. WHEN Rule_Engine assigns an incident a severity band of WATCH, WARNING, or EVACUATE, THE Alert_Agent SHALL compose, within 60 seconds of that assignment, one typed alert draft for each combination of configured channel and configured language, each draft containing the affected area list, exactly one recommended action, the nearest shelter holding available capacity above zero, the validity window expressed as a start timestamp and an end timestamp each carrying a time zone, and a confidence value between 0.0 and 1.0 inclusive.
2. WHERE no shelter holds available capacity above zero, THE Alert_Agent SHALL populate the shelter field of every alert draft with the configured no-capacity guidance and SHALL record the absence of available shelter capacity in Audit_Ledger.
3. THE Alert_Agent SHALL compose one alert variant in every language listed in the Community_Language_Configuration, which SHALL include Tamil and English, and SHALL label each variant with the language it is written in.
4. THE Alert_Agent SHALL constrain each channel variant to at most the configured character limit for that channel and SHALL make at most the configured recomposition attempt count when a composed variant exceeds that limit.
5. THE Alert_Agent SHALL submit every alert draft to Safety_QA_Agent before delivery and SHALL deliver only drafts for which Safety_QA_Agent returned a true pass indicator.
6. WHEN an incident severity band is EVACUATE, or the delivery audience recipient count exceeds the Escalation_Policy mass-notification audience threshold, THE Alert_Agent SHALL escalate the alert release to the coordinator through Escalation_Service carrying the Escalation_Policy response deadline for that severity band and the Escalation_Policy default action for an alert release.
7. WHILE an alert release escalation for an incident is unresolved, THE Alert_Agent SHALL withhold delivery of every variant of that alert.
8. IF the coordinator returns a decline response to an alert release escalation, THEN THE Alert_Agent SHALL deliver no variant of that alert and SHALL record the decline with the responding human identifier in Audit_Ledger.
9. IF a channel variant fails composition, fails delivery, or remains above the configured character limit after the configured recomposition attempt count, THEN THE Alert_Agent SHALL withhold that variant, SHALL continue delivery of the remaining variants, and SHALL record the withheld variant with the failure reason in Audit_Ledger.
10. WHEN an alert delivery is retried for the same incident, the same channel, the same language, and the same severity band, THE ThunAI_Platform SHALL deliver the alert content to each recipient of the delivery audience exactly once under an Idempotency_Key derived from the incident identifier, the channel, the language, and the severity band (correctness property: idempotent delivery).
11. THE Alert_Agent SHALL record every delivered alert in Audit_Ledger with the channel, the language, the recipient audience size, the delivery timestamp, the delivery outcome, and the approving human identifier where an approval applied.

### Requirement 8: Community Safety Knowledge Answers

**User Story:** As a resident, I want reliable answers to practical safety questions during an event, so that the coordinator's phone is not the only source of basic information.

#### Acceptance Criteria

1. WHEN Knowledge_Agent receives a resident safety question, THE Knowledge_Agent SHALL retrieve from the curated community knowledge source at most the configured maximum passage count, each retrieved passage carrying a relevance score, and SHALL return a response within 10 seconds of receipt.
2. WHEN Knowledge_Agent returns an answer, THE Knowledge_Agent SHALL include one citation for each source passage used, each citation carrying the passage identifier and the knowledge source version.
3. THE Knowledge_Agent SHALL include in an answer no statement that is not supported by a cited retrieved passage (correctness property: grounded answers).
4. WHEN Knowledge_Agent returns an answer and the detected question language is listed in the Community_Language_Configuration, THE Knowledge_Agent SHALL compose that answer in the detected language.
5. IF the question language is not detected or is not listed in the Community_Language_Configuration, THEN THE Knowledge_Agent SHALL compose the answer in the configured default language and SHALL state in the answer that the question language was not recognised.
6. IF no retrieved passage carries a relevance score at or above the configured relevance threshold, THEN THE Knowledge_Agent SHALL return a no-answer response that contains no safety guidance and SHALL route the question to the coordinator through Escalation_Service.
7. IF retrieval from the curated community knowledge source fails or does not complete within the configured retrieval timeout, THEN THE Knowledge_Agent SHALL return a response indicating that the knowledge source is unavailable and SHALL route the question to the coordinator through Escalation_Service.
8. WHEN an answer concerns a configured advisory category, including medical treatment, structural safety, and legal obligation, THE Knowledge_Agent SHALL prefix that answer with the configured advisory disclaimer rendered in the answer language.
9. THE Knowledge_Agent SHALL record in Audit_Ledger, for every resident safety question, the question, the detected language, the retrieved passage identifiers with their relevance scores, the returned answer or the no-answer outcome, and any coordinator routing performed.

### Requirement 9: Safety and Quality Review Before Release

**User Story:** As the ward coordinator, I want every outbound message and every irreversible action checked against a published policy, so that ThunAI does not send something harmful on my behalf.

#### Acceptance Criteria

1. WHEN Safety_QA_Agent receives a proposed outbound communication, THE Safety_QA_Agent SHALL return, within 10 seconds of receipt, a review result containing the reviewed content identifier, a pass indicator, the identifier of each violated policy, a suggested revision for each violated policy, and the applied policy version.
2. IF a review result for a proposed outbound communication or for a proposed Irreversible_Action carries a false pass indicator, THEN THE ThunAI_Platform SHALL withhold the corresponding delivery or execution, SHALL retain the proposed content and its review result unchanged, and SHALL escalate the proposal to the coordinator through Escalation_Service with the violated policy identifiers and the suggested revisions.
3. THE Safety_QA_Agent SHALL check every proposed outbound communication for resident names, resident contact numbers, and resident dwelling addresses, and SHALL mark each occurrence as a policy violation carrying the violated policy identifier.
4. THE ThunAI_Platform SHALL apply the configured content guardrail to every model invocation that processes resident-supplied text and to every model invocation that processes resident-supplied image content.
5. THE Safety_QA_Agent SHALL record every review result in Audit_Ledger containing the reviewed content identifier, the pass indicator, the violated policy identifiers, the applied policy version, and the review timestamp.
6. WHEN a component of ThunAI_Platform proposes an Irreversible_Action, THE Safety_QA_Agent SHALL review the proposed action against the active safety policy before execution and SHALL return a review result containing a pass indicator, the identifier of each violated policy, and the applied policy version.
7. IF Safety_QA_Agent returns no review result within 10 seconds of receipt, returns a review result without a pass indicator, or the configured content guardrail is unavailable or blocks the invocation, THEN THE ThunAI_Platform SHALL withhold the corresponding delivery and execution, SHALL record the withholding and its reason in Audit_Ledger, and SHALL escalate the withheld proposal to the coordinator through Escalation_Service.
8. THE ThunAI_Platform SHALL publish the active safety policy, including every policy identifier and the policy version, to Coordinator_Console.

### Requirement 10: Escalation Policy and Typed Decisions

**User Story:** As a judge evaluating design, I want the rule that decides between acting and asking to be stated in one readable place, so that the autonomy claim is verifiable rather than asserted.

#### Acceptance Criteria

1. THE Escalation_Policy SHALL reside in one configuration module and SHALL contain, as separately named entries each carrying a declared unit and a declared permitted range: the confidence floor as a value between 0.0 and 1.0 inclusive, the mass-notification audience threshold as a recipient count of 1 or greater, the anomaly ratio threshold as a multiple of the stored baseline of 1.0 or greater, the always-ask category set, the never-ask category set, the response deadline in whole minutes between 1 and 120 for each severity band in the ordered set NORMAL, WATCH, WARNING, EVACUATE, and the default action for each escalation type that ThunAI_Platform can raise.
2. THE Escalation_Policy SHALL carry a non-empty one-sentence rationale for each entry named in criterion 1 and SHALL carry a policy version identifier that changes whenever any entry value changes.
3. THE ThunAI_Platform SHALL express every hazard assessment, emergency request, dispatch decision, alert draft, and safety review as a typed structured output model carrying a confidence value between 0.0 and 1.0 inclusive and an action field holding exactly one value from a declared closed value set that includes an explicit ask-human value.
4. IF a typed decision confidence value is below the Escalation_Policy confidence floor, THEN THE ThunAI_Platform SHALL escalate that decision to a human and SHALL withhold the corresponding action until a human response arrives or the Escalation_Policy default action for that escalation type is applied.
5. IF a typed decision action field holds the ask-human value, THEN THE ThunAI_Platform SHALL escalate that decision to a human regardless of the confidence value and SHALL withhold the corresponding action until a human response arrives or the Escalation_Policy default action for that escalation type is applied.
6. THE ThunAI_Platform SHALL escalate every Irreversible_Action to a human before execution, and SHALL apply this escalation in preference to every never-ask entry of Escalation_Policy where an action falls in both.
7. WHERE an action falls in the Escalation_Policy never-ask category set, is not an Irreversible_Action, and does not fall in the Escalation_Policy always-ask category set, THE ThunAI_Platform SHALL execute that action without escalation.
8. THE ThunAI_Platform SHALL read every threshold used in an autonomy decision from the Escalation_Policy module, SHALL hold no autonomy threshold value elsewhere in the source tree, and SHALL record with every autonomy decision in Audit_Ledger the policy version identifier, the entry that determined the outcome, and whether the outcome was execution or escalation.
9. THE ThunAI_Platform SHALL publish to Coordinator_Console every Escalation_Policy entry named in criterion 1 with its unit, its rationale, and the active policy version identifier, and SHALL reflect a change to any published value within 5 seconds of the change.
10. IF Escalation_Policy omits an entry named in criterion 1, holds a value outside that entry's declared range, lists the same category in both the always-ask set and the never-ask set, or declares a response deadline for a higher severity band that exceeds the deadline declared for a lower severity band, THEN THE ThunAI_Platform SHALL execute no action without human approval, SHALL record the policy defect in Audit_Ledger, and SHALL notify the coordinator that the policy is unusable.
11. IF a typed decision omits the confidence value, omits the action field, carries a confidence value outside 0.0 to 1.0 inclusive, or carries an action value outside the declared value set, THEN THE ThunAI_Platform SHALL treat the decision as an ask-human decision, SHALL withhold the corresponding action, and SHALL record the validation failure in Audit_Ledger with the decision identifier.
12. THE Escalation_Policy SHALL declare the default action for every escalation type raised for an Irreversible_Action as withholding that action.

### Requirement 11: Escalation Delivery, Human Response, and Resume

**User Story:** As the ward coordinator, I want to be reached wherever I am with one clear question and one tap to answer, so that a paused agent run finishes without me reconstructing its context.

#### Acceptance Criteria

1. WHEN an agent run pauses for a human decision, THE Escalation_Service SHALL persist, within 5 seconds of the pause, an Escalation_Record with status OPEN containing the interrupt identifier, the incident identifier, a decision summary of one sentence not exceeding 200 characters, the reason for asking, the stakes, between 2 and 5 available options each carrying a distinct option identifier, exactly one default action drawn from the Escalation_Policy default action for that escalation type, and a response deadline computed as an absolute timestamp with time zone from the Escalation_Policy response deadline for the incident severity band.
2. WHEN an Escalation_Record is persisted, THE Escalation_Service SHALL publish that Escalation_Record to Decision_Inbox within 10 seconds of persistence and SHALL deliver a notification for that Escalation_Record to the configured out-of-band channel within 10 seconds of persistence.
3. THE Escalation_Service SHALL compose each escalation notification from a hand-authored template whose fields are populated only from the persisted Escalation_Record values, with no field generated by a model invocation, and that states what ThunAI decided, why a human is being asked, the stakes including any quantity and the response deadline as an absolute timestamp with time zone, the default action that applies if no response arrives by that deadline, and one selectable control per available option.
4. WHILE an Escalation_Record status is OPEN, WHEN a human submits a response whose value matches one of the persisted option identifiers of that Escalation_Record, THE Escalation_Service SHALL set that Escalation_Record status to RESOLVED with the submitted option identifier, SHALL resume the paused agent run using the persisted interrupt identifier within 10 seconds of receipt, and SHALL complete the remaining actions of that run.
5. THE Escalation_Service SHALL resume a paused agent run in a process other than the process in which the run paused, using only state persisted in State_Store and Memory_Store, including after the pausing process has terminated.
6. WHEN the response deadline of an Escalation_Record with status OPEN passes without a human response, THE Escalation_Service SHALL, within 60 seconds of that deadline, set that Escalation_Record status to RESOLVED_BY_DEFAULT, SHALL apply the Escalation_Policy default action for that escalation type, SHALL record the timeout and the applied default action in Audit_Ledger, and SHALL notify the coordinator of the applied default action.
7. WHEN a response is submitted for an Escalation_Record whose status is RESOLVED or RESOLVED_BY_DEFAULT, THE Escalation_Service SHALL leave the recorded resolution, the resolution timestamp, and all state changes produced by that resolution unchanged, SHALL execute no further action of the associated run, SHALL return the recorded resolution and its resolution timestamp to the submitter, and SHALL record the duplicate submission in Audit_Ledger (correctness property: idempotent resolution).
8. THE Escalation_Service SHALL record in Audit_Ledger, for every Escalation_Record that reaches status RESOLVED or RESOLVED_BY_DEFAULT, the responding human identifier where a human responded, the selected option identifier, the response value, the response timestamp, and whether the resolution came from a human response or from an applied default action.
9. THE ThunAI_Platform SHALL raise at most one Escalation_Record per routine incident sweep run over the seeded demonstration dataset and SHALL complete every remaining item of that sweep run without raising an Escalation_Record.
10. IF a submitted response value matches none of the persisted option identifiers of the addressed Escalation_Record, THEN THE Escalation_Service SHALL reject the response, SHALL leave the Escalation_Record status, response deadline, and default action unchanged, SHALL resume no agent run, SHALL return an error indication naming the accepted option identifiers, and SHALL record the rejected submission in Audit_Ledger.
11. IF every delivery attempt to the configured out-of-band channel fails after 3 attempts within 60 seconds of the first attempt, THEN THE Escalation_Service SHALL record the delivery failure in Audit_Ledger, SHALL leave the Escalation_Record status OPEN with its response deadline and default action unchanged, and SHALL present the delivery-failure state on that Escalation_Record in Decision_Inbox.
12. IF resuming a paused agent run fails after a resolution is recorded, THEN THE Escalation_Service SHALL leave the recorded resolution unchanged, SHALL record the resume failure in Audit_Ledger with the interrupt identifier and the actions not completed, and SHALL notify the coordinator that the resolved decision was not carried out.

### Requirement 12: Coordinator Console

**User Story:** As the ward coordinator, I want one screen showing what ThunAI did, what it is waiting on, and what it is asking me, so that I can supervise the ward without collating three tools.

#### Acceptance Criteria

1. THE Coordinator_Console SHALL present Decision_Inbox listing every open Escalation_Record ordered by response deadline from soonest to latest, each entry showing the decision summary, the reason for asking, the stakes, the default action, the remaining time before the response deadline expressed in whole minutes and seconds, and one control per available option, with the remaining time recomputed at least once every 10 seconds.
2. WHEN the coordinator activates an option control in Decision_Inbox, THE Coordinator_Console SHALL submit that option to Escalation_Service as the response to the identified Escalation_Record on a single activation, and SHALL disable every option control of that Escalation_Record until Escalation_Service confirms an outcome, so that one activation produces at most one submitted response.
3. IF Escalation_Service returns an error for a submitted response or confirms no outcome within 10 seconds of submission, THEN THE Coordinator_Console SHALL retain the Escalation_Record in Decision_Inbox as unresolved, SHALL present an error indication stating that the response was not recorded, and SHALL re-enable the option controls of that Escalation_Record for retry.
4. THE Coordinator_Console SHALL present every open incident, ordered by severity band from highest to lowest and then by most recent update, each incident showing the severity band, the affected areas, the count of open requests, the count of assigned responders, and the shelter availability expressed as unoccupied places and total capacity per shelter.
5. WHEN State_Store commits a change to an incident, a request, a responder status, a shelter capacity, or an Escalation_Record, THE Coordinator_Console SHALL reflect that change in the presented views within 5 seconds of the commit and without a manual page reload.
6. IF the Coordinator_Console receives no update confirmation from State_Store for more than 15 seconds, THEN THE Coordinator_Console SHALL present a staleness indication stating the age of the displayed data in seconds and SHALL continue attempting to resume updates until updates resume or the coordinator leaves the view.
7. THE Coordinator_Console SHALL present the Audit_Ledger entries for a selected incident ordered by entry timestamp from oldest to newest, each entry showing the entry timestamp, the tool name, the tool inputs with direct personal identifiers excluded, the outcome, and the approving human identifier where an approval applied.
8. THE Coordinator_Console SHALL present, for the 20 most recent runs ordered most recent first, the run token count as a whole number, the run latency in milliseconds, the model identifier, and the estimated cost in the configured currency with that currency identified.
9. THE Coordinator_Console SHALL restrict every view and every action to authenticated members of the coordinator group.
10. IF a request to Coordinator_Console carries no valid authentication or carries an authenticated identity outside the coordinator group, THEN THE Coordinator_Console SHALL deny the request, SHALL present an indication that coordinator authorisation is required, and SHALL disclose no incident, request, responder, resident, or Escalation_Record content.
11. THE Coordinator_Console SHALL provide a conversational panel backed by Coordinator_Orchestrator as a secondary surface alongside Decision_Inbox, and SHALL keep Decision_Inbox and the open incident list operable while the conversational panel is unavailable.
12. THE Coordinator_Console SHALL meet WCAG 2.1 Level AA success criteria for keyboard operability, focus visibility, text contrast, and programmatic name and role of every interactive control.

### Requirement 13: Responder Interface

**User Story:** As a registered responder, I want to accept an assignment and report progress in two taps, so that the coordinator sees my status without calling me.

#### Acceptance Criteria

1. WHEN a responder assignment is persisted to State_Store, THE ThunAI_Platform SHALL deliver an assignment notification to that responder within 10 seconds containing the request identifier, the location reference, the occupant count, the mobility-assistance indicator, the medical-need indicator, the equipment requirement, and the acknowledgement deadline timestamp.
2. THE Responder_Interface SHALL present the authenticated responder's current assignment with the fields carried in the assignment notification, SHALL make every status control permitted by the current assignment state reachable in one control activation, and SHALL enable the accept and decline controls only while the assignment is awaiting acknowledgement, the en-route control only after an accept response, the on-scene control only after an en-route response, and the completed control only after an on-scene response.
3. WHEN a responder submits a decline response, THE ThunAI_Platform SHALL return that responder availability status to AVAILABLE, SHALL start a new dispatch decision for the same request with that responder excluded from the candidate set for that request, and SHALL record the decline with its submission timestamp in Audit_Ledger.
4. WHEN a responder submits a completed response, THE ThunAI_Platform SHALL transition the request to the verification state, SHALL return that responder availability status to AVAILABLE, and SHALL record the completion timestamp in Audit_Ledger.
5. WHEN a request enters the verification state, THE ThunAI_Platform SHALL escalate the request closure to the coordinator through Escalation_Service containing the request identifier, the completing responder identifier, and the completion timestamp, and SHALL withhold the transition to RESOLVED until an approval response arrives.
6. THE Responder_Interface SHALL restrict access to authenticated members of the responder group, SHALL present only the assignments whose assigned responder identifier equals the authenticated responder identifier, and SHALL deny every other assignment request with an authorisation error indication and no assignment content.
7. IF the assignment notification delivery fails, or the responder submits no accept response within the acknowledgement window measured from delivery of that notification, THEN THE ThunAI_Platform SHALL return that responder availability status to AVAILABLE, SHALL start a new dispatch decision for the same request with that responder excluded from the candidate set for that request, and SHALL record the non-acknowledgement and its cause in Audit_Ledger, where the acknowledgement window is configured in the range 60 to 600 seconds with a default of 120 seconds.
8. WHEN a responder submits an en-route response or an on-scene response, THE ThunAI_Platform SHALL record the resulting assignment state with its submission timestamp in State_Store and in Audit_Ledger and SHALL leave that responder availability status at ASSIGNED.
9. IF a responder submits a status response that the current assignment state does not permit, THEN THE ThunAI_Platform SHALL reject the response, SHALL leave the assignment state, the request state, and the responder availability status unchanged, and SHALL return an error indication naming the responses permitted from the current assignment state.
10. IF the coordinator submits a decline response to a request-closure escalation, THEN THE ThunAI_Platform SHALL leave the request outside the RESOLVED state, SHALL return the request to the dispatch-eligible state, and SHALL record the closure rejection in Audit_Ledger.

### Requirement 14: Resident Status Page

**User Story:** As a resident, I want a public page with the current hazard status and shelter availability, so that I do not have to message anyone to learn whether my area is affected.

#### Acceptance Criteria

1. THE Resident_Status_Page SHALL present, without requiring authentication, the current severity band as exactly one value from the ordered set NORMAL, WATCH, WARNING, EVACUATE, the affected area list, the recommended action for that severity band, and the timestamp of the reading that produced that severity band expressed in the configured community time zone.
2. THE Resident_Status_Page SHALL present every shelter recorded in State_Store with the shelter name, the location reference, the available capacity as a whole number of spaces not below zero, and a full indicator for each shelter whose available capacity is zero.
3. WHEN State_Store changes the severity band, the affected area list, or a shelter available capacity, THE Resident_Status_Page SHALL reflect the change within 15 seconds of the change and without a manual page reload.
4. THE Resident_Status_Page SHALL present every field named in criteria 1, 2, and 6 in every language listed in the Community_Language_Configuration, SHALL render the configured default language on initial load, and SHALL provide a language selection control that applies the selected language to every presented field within 2 seconds of activation.
5. THE Resident_Status_Page SHALL exclude every resident name, every resident contact identifier, and every household-level location detail, and SHALL present emergency request information only as aggregate counts per affected area.
6. THE Resident_Status_Page SHALL present the configured advisory notice stating that ThunAI_Platform is a community coordination aid and that official emergency instructions take precedence, in the selected language, on initial load and without requiring user interaction to reveal it.
7. THE Resident_Status_Page SHALL meet WCAG 2.1 Level AA success criteria for text contrast, keyboard operability, and text alternatives for non-text content, in every language listed in the Community_Language_Configuration.
8. IF the reading that produced the current severity band is older than the configured staleness limit or is marked unavailable, THEN THE Resident_Status_Page SHALL present the last known severity band together with the age of that reading and an indication that the reading is not current.
9. IF Resident_Status_Page cannot retrieve current state from State_Store, THEN THE Resident_Status_Page SHALL present the most recently retrieved values, the timestamp of that retrieval, and an indication that the presented values may be out of date, and SHALL continue retrieval attempts at the configured refresh interval.
10. IF translated content for the selected language is unavailable for a presented field, THEN THE Resident_Status_Page SHALL present that field in the configured default language and SHALL indicate that the translation for that field is unavailable.

### Requirement 15: Shared Live State, Idempotency, and Resumability

**User Story:** As the ward coordinator, I want the system's picture of the ward to be single, current, and safe to re-run, so that a retry never double-dispatches or double-notifies.

#### Acceptance Criteria

1. THE State_Store SHALL hold incidents, emergency requests, responders, shelters, Escalation_Records, processed trigger event identifiers, Incident_Graph relationships, and Audit_Ledger entries under one queryable data model in which each entity is addressable by a single unique identifier and every read of an entity returns that entity's most recently committed version.
2. THE ThunAI_Platform SHALL accept an Idempotency_Key on every write operation to State_Store and SHALL leave the end state of the addressed entity unchanged when the same Idempotency_Key is presented again within a retention window of at least 72 hours, returning the outcome recorded for the first presentation (correctness property: idempotence).
3. WHEN a trigger event is received, THE ThunAI_Platform SHALL record the trigger event identifier in State_Store before performing any downstream write for that event and SHALL retain that identifier for at least 72 hours.
4. IF a received trigger event identifier is already recorded in State_Store, THEN THE ThunAI_Platform SHALL skip all processing for that event, perform no dispatch and no notification, and append a duplicate-event skip entry to Audit_Ledger.
5. THE ThunAI_Platform SHALL persist run progress per incident after each completed step and SHALL continue an interrupted run from the last persisted step rather than from the run start.
6. THE Audit_Ledger SHALL accept appends only and SHALL reject any update or deletion of an existing entry with an error indicating that the ledger is append-only, leaving the existing entry unchanged.
7. THE ThunAI_Platform SHALL apply a state transition to a request only along the transitions declared in the Request_Lifecycle and SHALL reject any other transition with an error identifying the current state and the requested state, leaving the request in its current state (correctness property: valid transitions only).
8. THE ThunAI_Platform SHALL keep the sum of assigned shelter placements at or below the shelter total capacity for every shelter, including when two or more placement writes for the same shelter are submitted concurrently, and SHALL reject any placement write that would raise the sum above the total capacity while leaving the shelter's committed placements unchanged (correctness property: capacity invariant).
9. IF a write to State_Store fails on 3 successive attempts within 30 seconds, THEN THE ThunAI_Platform SHALL leave the addressed entity unchanged, append the failure to Audit_Ledger, and escalate a state-write failure to the coordinator as an Escalation_Record through Escalation_Service within 60 seconds of the final failed attempt.
10. IF a write operation to State_Store is submitted without an Idempotency_Key, THEN THE ThunAI_Platform SHALL reject the write with an error indicating the missing Idempotency_Key and SHALL leave State_Store unchanged.
11. IF persisted run progress for an incident is absent or unreadable when an interrupted run resumes, THEN THE ThunAI_Platform SHALL skip every step whose Idempotency_Key is already recorded in State_Store and SHALL escalate a resume-integrity failure to the coordinator as an Escalation_Record through Escalation_Service.

### Requirement 16: Reliability Harness Enforced by Hooks

**User Story:** As a judge evaluating technical implementation, I want the guardrails implemented as deterministic code on the agent lifecycle, so that the limits hold regardless of what the model decides.

#### Acceptance Criteria

1. THE Harness SHALL register a lifecycle hook on the before-tool-call event of every agent in ThunAI_Platform, and that hook SHALL evaluate every tool call before the tool executes.
2. IF a tool call is declared a write tool call, falls outside the Escalation_Policy never-ask category, and carries no recorded approval response bound to the same run identifier and the same tool call identifier, THEN THE Harness SHALL block the tool call and SHALL prevent the tool from executing.
3. WHEN the Harness blocks a tool call, THE Harness SHALL return a message to the model containing the tool name, the specific gate or limit that caused the block, and the expected next step, and SHALL leave the run active rather than terminating it with an error.
4. THE Harness SHALL enforce a configured maximum invocation count per tool per run, configurable as a whole number in the range 1 to 50 with a default of 5, SHALL count invocations against the current run identifier starting from zero at run start, and SHALL block every further invocation of a tool that reaches that count for the remainder of that run.
5. THE Harness SHALL enforce a configured maximum estimated model spend per run, expressed in whole minor currency units of the configured reporting currency and configurable in the range 1 to 100000 with a default of 200, SHALL accumulate the estimated spend of every model invocation in the run, and SHALL block every further model invocation in that run once the accumulated estimated spend reaches that maximum.
6. THE Harness SHALL enforce a configured maximum outbound notification count per run, configurable as a whole number in the range 1 to 100 with a default of 20, SHALL count outbound notifications against the current run identifier starting from zero at run start, and SHALL block every further notification tool call in that run once the count reaches that maximum.
7. WHEN the Harness blocks a tool call or a model invocation because a configured maximum was reached, THE Harness SHALL append a limit-breach entry to Audit_Ledger, SHALL mark the run outcome as partial, and SHALL deliver a limit-breach notification to the coordinator through Escalation_Service.
8. THE Harness SHALL append one entry to Audit_Ledger for every tool call, including every blocked tool call, within 1 second of that tool call completing or being blocked, containing the timestamp, the run identifier, the tool call identifier, the tool name, the inputs, the outcome or the block reason, the approving human identifier where an approval applied, the model identifier, and the input and output token counts of the model invocation that produced the tool call.
9. IF the Audit_Ledger append for a write tool call does not succeed, THEN THE Harness SHALL block that tool call, SHALL prevent the tool from executing, and SHALL deliver an audit-capture failure notification to the coordinator through Escalation_Service.
10. THE Harness SHALL replace the value of every configured personal-identifier field and every configured secret field with a non-identifying placeholder before an Audit_Ledger entry, a log record, or a trace attribute is persisted or emitted, and SHALL emit no unredacted value of those fields in any such record (correctness property: redaction completeness).
11. THE Harness SHALL enforce every limit in this requirement through registered lifecycle hooks that execute independently of model output and SHALL rely on no system prompt instruction for enforcement, and two evaluations receiving identical tool call inputs, identical counter states, and identical configuration values SHALL produce identical enforcement decisions (correctness property: deterministic enforcement).

### Requirement 17: Memory Across Separate Runs

**User Story:** As the ward coordinator, I want ThunAI to remember what happened yesterday and what I decided last time, so that each run builds on the last instead of starting blank.

#### Acceptance Criteria

1. THE Memory_Store SHALL persist conversation state and durable community facts under one session identifier per ward-level conversation and one distinct agent identifier per agent role, and SHALL commit every update before the run that produced the update reports completion.
2. WHEN a run starts in a process other than the process of the prior run, THE ThunAI_Platform SHALL retrieve, within 10 seconds of run start and for the same session identifier, the prior hazard readings, the prior incident outcomes, and the persisted coordinator preferences written by the prior run.
3. THE Memory_Store SHALL retain the rolling baseline of hazard readings for the most recent 30 days and SHALL supply that baseline, together with the count of readings it contains, to Monitor_Agent for anomaly comparison.
4. WHEN a coordinator response to an Escalation_Record states an explicit directive in one of the configured preference categories, THE ThunAI_Platform SHALL persist that directive as a preference carrying its category, the responding coordinator identifier, and the response timestamp, and SHALL supersede any earlier persisted preference in the same category.
5. WHEN a run evaluates hazard readings identical to the readings of a prior run and a persisted coordinator preference applies to those readings, THE ThunAI_Platform SHALL apply the persisted preference, SHALL produce the autonomous outcome directed by that preference in place of the outcome recorded for the prior run, and SHALL record both outcomes and the applied preference in Audit_Ledger.
6. IF a run attempts to write Memory_Store under a session identifier and agent identifier pair already held by an in-progress run, THEN THE Memory_Store SHALL reject the write, SHALL leave the state persisted by the in-progress run unchanged, and SHALL return a concurrent-session error indication to the requesting run.
7. WHEN the retained conversation context for an incident reaches the configured context token budget, THE ThunAI_Platform SHALL reduce the retained context using the configured context management strategy while preserving every recorded decision, every applied preference, and every open Escalation_Record reference for that incident.
8. IF Memory_Store holds no prior state for the session identifier, THEN THE ThunAI_Platform SHALL continue the run with the prior readings marked absent, SHALL record the absent-prior-state condition in Audit_Ledger, and SHALL withhold any autonomous suppression of an alert that depends on baseline comparison during that run.
9. IF the retained baseline for a river reach contains fewer than 7 readings, THEN THE Memory_Store SHALL supply that baseline with an insufficient-baseline indicator and SHALL exclude that baseline from anomaly comparison.
10. IF retrieval from Memory_Store fails after the configured retry count, THEN THE ThunAI_Platform SHALL record the retrieval failure in Audit_Ledger, SHALL escalate a memory-unavailable decision to the coordinator, and SHALL leave the persisted state unchanged.

### Requirement 18: Safety, Privacy, and Enforced Boundaries

**User Story:** As a member of the ward committee, I want the system to handle data about vulnerable neighbours with restraint, so that adopting ThunAI does not create a new risk for the people it serves.

#### Acceptance Criteria

1. THE ThunAI_Platform SHALL populate every committed fixture, every automated test input, and every demonstration dataset with synthetic resident records only, SHALL include no record derived from an identifiable living resident, and SHALL display the configured synthetic-data statement on every Coordinator_Console view that presents resident data and in the repository README introduction.
2. THE ThunAI_Platform SHALL persist only the resident fields named in the committed data-minimisation field list, SHALL limit that list to the fields required to complete a dispatch and to answer the resident, and SHALL persist no resident field absent from that list.
3. THE ThunAI_Platform SHALL grant each cloud execution role only the actions and the named data stores, secrets, and model identifiers that the role invokes, and SHALL grant no execution role a wildcard resource scope or an administrator-equivalent permission set.
4. THE ThunAI_Platform SHALL retrieve every external credential from a managed secret store or an identity provider reference at run time, and SHALL hold no credential value in the source tree, the committed configuration, the committed fixtures, the container image, or the commit history.
5. THE ThunAI_Platform SHALL execute an Irreversible_Action only after an approval response for the corresponding Escalation_Record is recorded in Audit_Ledger together with the approving human identifier and the approval timestamp.
6. WHEN Knowledge_Agent returns content concerning medical treatment, structural safety, or legal obligation, THE ThunAI_Platform SHALL include the configured advisory disclaimer and a referral to a qualified authority in that content, and SHALL exclude any directive instructing the resident to perform a medical procedure or a structural repair.
7. THE ThunAI_Platform SHALL connect only to the model context protocol servers declared in the committed configuration file, and SHALL refuse and record in Audit_Ledger every connection attempt to a server absent from that file, including a server named in model output.
8. THE repository README SHALL contain a boundaries section listing every Irreversible_Action category, every other action ThunAI_Platform declines to perform autonomously, the enforcement mechanism for each, and the acceptance criterion or test that verifies each enforcement mechanism.
9. WHEN the configured resident free-text retention window elapses for a stored resident message, THE ThunAI_Platform SHALL delete that free-text content within 60 minutes of the elapse, SHALL retain the typed emergency request without the free text, and SHALL append a deletion entry to Audit_Ledger that names the request identifier and excludes the deleted content, where the retention window is configured in whole hours between 1 hour and 720 hours.
10. IF retrieval of an external credential fails, THEN THE ThunAI_Platform SHALL block the dependent tool call, SHALL leave State_Store unchanged for that call, SHALL append a credential-unavailable entry to Audit_Ledger that contains no credential value or partial credential value, and SHALL escalate a dependency-unavailable decision to the coordinator.
11. IF a human declines an Irreversible_Action, or the Escalation_Record for an Irreversible_Action reaches its response deadline with no response, THEN THE ThunAI_Platform SHALL apply non-execution as the default action, SHALL leave every affected State_Store record in its pre-approval state, and SHALL append a cancellation entry to Audit_Ledger stating the decline reason or the timeout.

### Requirement 19: Deployment and Reproducibility

**User Story:** As a judge with a clean machine, I want to clone the repository and reach a working system without collecting six third-party credentials, so that I can evaluate what the project actually does.

#### Acceptance Criteria

1. THE Deployment_App SHALL provision the agent runtime, the agent memory resource, the agent gateway and gateway targets, State_Store, the real-time push interface, the authentication provider, the trigger schedules, the ingestion endpoint, the notification channels, and the web application hosting from one Python AWS CDK application, with no manual console step required for any of these resources.
2. THE Deployment_App SHALL produce an agent runtime artefact built for the arm64 architecture and SHALL abort the deployment, before the agent runtime resource is created, with an error indicating an architecture mismatch if the built artefact targets any other architecture.
3. THE ThunAI_Platform SHALL expose exactly one invocation endpoint and exactly one health endpoint on the port required by the agent runtime contract.
4. WHEN the ThunAI_Platform invokes the agent runtime, THE ThunAI_Platform SHALL supply a runtime session identifier of 33 to 128 characters that is distinct from the identifier supplied for every earlier invocation.
5. WHERE the fixtures configuration flag is enabled, THE ThunAI_Platform SHALL resolve every external interface to Fixture_Backend and SHALL complete the full trigger-to-resolution loop with zero third-party credentials configured and zero outbound calls to any third-party service other than the configured model provider.
6. THE ThunAI_Platform SHALL enable the fixtures configuration flag by default in the committed example environment file, and that file SHALL list every configuration variable the system reads with empty or synthetic values and no real credential value.
7. THE ThunAI_Platform SHALL provide a seed script that, in one command and with no interactive input, resets State_Store and Memory_Store to the committed demonstration state, and repeated runs of the seed script SHALL produce the same demonstration state as a single run.
8. THE ThunAI_Platform SHALL pin every Python dependency and every JavaScript dependency to a single exact version in a committed manifest, with no version range and no floating specifier.
9. THE ThunAI_Platform SHALL read the model identifier, the region, and every escalation threshold from configuration, SHALL hold the model identifier in exactly one configuration module, and SHALL validate these configuration values before the first model invocation, rejecting the run with an error identifying each absent value.
10. THE repository README SHALL contain ordered setup steps that reach a completed demonstration run on a clean clone in 15 minutes or less, where no step depends on information that is absent from the README.
11. WHEN the health endpoint receives a request, THE ThunAI_Platform SHALL return a healthy status payload within 1000 milliseconds.
12. THE ThunAI_Platform SHALL provide a demonstration script that runs the complete trigger-to-resolution loop in one command with no interactive input, completes within 600 seconds, and reports a failure indication naming the first incomplete step if any step of the loop does not complete.
13. IF the fixtures configuration flag is disabled and a credential required by an external interface is absent, THEN THE ThunAI_Platform SHALL abort the run before the first call to that interface, SHALL report an error naming each absent credential, and SHALL leave State_Store unchanged.

### Requirement 20: Observability and Cost Control

**User Story:** As the ward coordinator, I want to see what each run cost and how long it took, so that I can tell whether running ThunAI is sustainable for a volunteer committee.

#### Acceptance Criteria

1. THE Observability_Layer SHALL emit exactly one trace per run of ThunAI_Platform, containing one span per agent node execution, one span per tool call, and one span per model invocation, and each span SHALL carry a start timestamp, a duration in milliseconds, and a completion status of success or failure.
2. THE Observability_Layer SHALL attach the run identifier, the session identifier, and either the incident identifier or an explicit no-incident value to every emitted trace, and SHALL record no resident free-text content and no credential value in any span attribute.
3. WHEN a run reaches a terminal status, THE ThunAI_Platform SHALL record for that run the input token count, the output token count, the wall-clock latency in milliseconds, every invoked model identifier, and the estimated cost in the configured currency unit derived from the recorded token counts and the configured per-token price rate of each invoked model identifier, and SHALL expose the recorded values to Coordinator_Console within 5 seconds of recording.
4. THE Deployment_App SHALL provision a budget alarm at the configured spend threshold, expressed in the configured currency unit and evaluated over the current billing period.
5. WHEN the accrued spend for the current billing period reaches the configured spend threshold, THE ThunAI_Platform SHALL deliver a budget notification to the configured address and SHALL deliver at most one budget notification per threshold per billing period.
6. THE ThunAI_Platform SHALL route every classification model invocation and every summarisation model invocation to the configured low-cost model identifier and every trade-off reasoning model invocation to the configured high-capability model identifier.
7. THE ThunAI_Platform SHALL bound every scheduled run with a configured wall-clock timeout expressed in seconds, whose configured value is at most 900 seconds.
8. IF a run reaches the configured wall-clock timeout, THEN THE ThunAI_Platform SHALL initiate no further tool call and no further model invocation for that run, SHALL record a terminal status indicating timeout, and SHALL retain every state change that the run already committed.
9. IF emission of a trace fails or recording of run metrics fails, THEN THE Observability_Layer SHALL record the emission failure and THE ThunAI_Platform SHALL continue the run to its terminal status with the run outcome unchanged.
10. THE repository README SHALL state the mean tokens per run, the mean wall-clock latency per run in seconds, the mean estimated cost per run in the configured currency unit, and the number of runs measured, measured over at least 10 runs of the seeded demonstration dataset.

### Requirement 21: Evaluation and Correctness Verification

**User Story:** As a judge, I want evidence that the behaviour holds across cases rather than in one recorded run, so that the reliability claims are checkable.

#### Acceptance Criteria

1. THE Eval_Suite SHALL contain at least 20 recorded scenarios, and the scenario set SHALL include at least one scenario for each of the severity bands NORMAL, WATCH, WARNING, and EVACUATE, at least one intake scenario for each language listed in the Community_Language_Configuration, at least one dispatch scenario with a capable available responder, at least one dispatch scenario with no capable available responder, at least one escalation approval scenario, at least one escalation decline scenario, and at least one escalation timeout scenario.
2. THE Eval_Suite SHALL compute the goal-success rate as the count of recorded scenarios for which every declared pass condition holds divided by the total recorded scenario count, expressed as a percentage to one decimal place, and THE repository README SHALL state that rate, the total scenario count, and the Eval_Suite version that produced them.
3. THE Eval_Suite SHALL verify that, for every pair of hazard reading sets that differ in exactly one reading and share the same Rule_Set_Version, where the differing reading value in the second set is greater than or equal to the value in the first set, the Rule_Engine severity band of the second set is at or above the severity band of the first set in the ordered set NORMAL, WATCH, WARNING, EVACUATE, with the generated reading values spanning the configured valid range of that reading in its configured unit and including the configured minimum, the configured maximum, each threshold boundary value, and the representable values immediately below and immediately above each threshold boundary value.
4. THE Eval_Suite SHALL verify that, for every State_Store write operation type and every Idempotency_Key, applying that write between 2 and 10 times produces an end state equal in every persisted field to the end state produced by applying that write once.
5. THE Eval_Suite SHALL verify that, at every point in every generated sequence of dispatch, accept, decline, non-acknowledgement, and completion events, no responder holds more than one assignment in an active state.
6. THE Eval_Suite SHALL verify that, at every point in every generated sequence of placement and release events, every shelter available capacity is an integer between 0 and that shelter total capacity inclusive, with the generated sequences including sequences that attempt a placement beyond the shelter total capacity and sequences that attempt a release beyond the recorded placement count for that shelter.
7. THE Eval_Suite SHALL verify that, for every typed decision model type and every generated instance of that type, deserialising the serialised form of the instance produces an instance whose every field value equals the corresponding field value of the original instance, with the generated instances including the confidence values 0.0 and 1.0, instances with every optional field absent, and instances whose text fields hold the configured maximum length for that field.
8. THE Eval_Suite SHALL verify that, for every generated sequence of request events, every request state transition applied by ThunAI_Platform belongs to the declared Request_Lifecycle, and that every attempted transition outside the declared Request_Lifecycle is rejected with the recorded request state unchanged.
9. THE Eval_Suite SHALL verify that, for every generated run whose typed decision confidence value is below the Escalation_Policy confidence floor, including the largest representable value below that floor, the run produces exactly one Escalation_Record for that decision and produces no Audit_Ledger entry for the corresponding action.
10. THE Eval_Suite SHALL execute against Fixture_Backend with no third-party credential, SHALL execute in a continuous integration workflow on every push to the default branch and on every pull request targeting the default branch, and SHALL complete one full execution within 20 minutes of wall-clock time.
11. THE Eval_Suite SHALL declare, for every recorded scenario, the scenario identifier, the input fixture identifier, the expected observable outcome, and the pass condition evaluated to determine scenario success.
12. THE Eval_Suite SHALL execute each correctness property in criteria 3 through 9 against at least 100 generated input cases per execution, SHALL bound every generated event sequence to between 1 and 50 events, and SHALL record the generator seed used for each execution.
13. IF a generated input case violates a correctness property or a recorded scenario fails a declared pass condition, THEN THE Eval_Suite SHALL report the violated property identifier or the failing scenario identifier, the reduced failing input case, and the generator seed, and SHALL return a failing execution status.

### Requirement 22: Submission Artefacts

**User Story:** As the project author, I want the submission package complete and consistent with the running system, so that the entry is judged on the product rather than rejected on paperwork.

#### Acceptance Criteria

1. THE repository SHALL contain a license file at the repository root whose content is the unmodified text of the MIT license.
2. THE repository host SHALL display the license name MIT in the repository About section.
3. THE repository README SHALL state exactly one track in its first line, chosen from the set Everyday Agents, Professional Agents, Good Neighbor Agents.
4. THE repository README SHALL contain one section for each of the following: the problem, the named audience, the quantified repetition stated as occurrences per week and minutes per occurrence, the autonomous behaviour, the Escalation_Policy thresholds with each configured value, the Strands feature usage, the setup steps, the configuration table listing every environment variable with its required-or-optional flag and its default value, the boundaries, and the limitations.
5. THE repository SHALL contain an architecture diagram image file in a raster or vector image format.
6. THE repository README SHALL display the architecture diagram image inline.
7. THE architecture diagram SHALL show the triggers, the agent tier with its tools, the escalation path to a human, the human response path back into the agent tier, and the persisted state.
8. THE architecture diagram SHALL label the escalation path with the governing Escalation_Policy rule, including the confidence floor value and the always-ask categories that trigger escalation.
9. THE repository SHALL contain a demonstration video script that presents, in order, the trigger, the autonomous handling, the escalation delivery, the human approval, the resumed completion, and the memory effect, that allocates at least 5 seconds to each of those six beats, that states the problem, the named audience, and the stakes within the first 40 seconds, and whose allocated durations sum to at most 300 seconds.
10. THE repository SHALL contain no credential value in the working tree, in the commit history, in the fixtures, or in the committed images.
11. IF the credential scan of the working tree and the commit history reports a match, THEN THE continuous integration workflow SHALL complete with a failed result identifying the matching path and revision and SHALL block the change from reaching the default branch.
12. THE repository README SHALL describe only behaviour that the committed code performs, and SHALL reference, for each described behaviour, the acceptance criterion or the Eval_Suite scenario that verifies it.
13. IF a behaviour is described in the repository README without a committed implementation, THEN THE repository README SHALL list that behaviour in its limitations section as not implemented.
14. THE repository and the demonstration video link SHALL be retrievable by an unauthenticated reader with no membership, invitation, or region restriction.
15. THE submission entry SHALL state the track named in the first line of the repository README, the repository URL, the demonstration video URL, and the AWS Builder ID.
