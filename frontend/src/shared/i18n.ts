/**
 * shared/i18n.ts — static UI-chrome translation via a keyed dictionary.
 *
 * One keyed dictionary per Community_Language_Configuration entry (Tamil,
 * English at minimum). The default language renders on initial load; a language
 * selection control applies the chosen language to every field (Req 14.4). When
 * a key is missing for the selected language, the field falls back to the
 * default language *and* the caller is told the translation was unavailable so
 * it can show a per-field indicator (Req 14.10).
 *
 * This is UI-chrome translation only — distinct from the LLM-composed
 * resident-facing content generated per-language at agent runtime (Design §3.8).
 */

import { useCallback, useState } from 'react';

/** Supported language codes. Extend as Community_Language_Configuration grows. */
export type LanguageCode = 'en' | 'ta';

/** Language rendered on initial load (Req 14.4). */
export const DEFAULT_LANGUAGE: LanguageCode = 'en';

/** Languages listed in Community_Language_Configuration, in display order. */
export const SUPPORTED_LANGUAGES: readonly LanguageCode[] = ['en', 'ta'];

export const LANGUAGE_LABELS: Record<LanguageCode, string> = {
  en: 'English',
  ta: 'தமிழ்',
};

/** Translation key. Keeping this a string keeps the dictionary open for growth. */
export type TranslationKey = string;

type Dictionary = Record<TranslationKey, string>;

/**
 * Keyed dictionaries. Every key present in the default language is the
 * translation contract; a missing key in a non-default language triggers the
 * documented per-field fallback (Req 14.10).
 */
const DICTIONARIES: Record<LanguageCode, Dictionary> = {
  en: {
    'status.title': 'Community Flood Status',
    'status.severity.label': 'Current status',
    'status.severity.NORMAL': 'Normal',
    'status.severity.WATCH': 'Watch',
    'status.severity.WARNING': 'Warning',
    'status.severity.EVACUATE': 'Evacuate',
    'status.severity.action.NORMAL': 'No action needed. Stay informed.',
    'status.severity.action.WATCH': 'Stay alert and monitor updates.',
    'status.severity.action.WARNING': 'Prepare to move. Keep an evacuation kit ready.',
    'status.severity.action.EVACUATE': 'Leave now and move to a listed shelter.',
    'status.affectedAreas': 'Affected areas',
    'status.affectedAreas.none': 'No areas currently reported affected',
    'status.affectedAreas.requestCount': 'reported requests',
    'status.recommendedAction': 'Recommended action',
    'status.readingTime': 'Reading time',
    'status.readingStale': 'This reading is not current',
    'status.readingAge': 'Reading age',
    'status.dataMayBeOutdated': 'These values may be out of date',
    'status.lastRetrieved': 'Last retrieved',
    'shelters.title': 'Shelters',
    'shelters.name': 'Shelter',
    'shelters.location': 'Location',
    'shelters.available': 'Available places',
    'shelters.full': 'Full',
    'shelters.none': 'No shelters listed',
    'ruleset.title': 'Active rule set',
    'ruleset.threshold': 'Threshold',
    'ruleset.value': 'Value',
    'ruleset.stalenessLimit': 'Staleness limit',
    'ruleset.version': 'Rule set version',
    'advisory.title': 'Advisory',
    'advisory.notice':
      'ThunAI is a community coordination aid, not an official emergency service. Official emergency instructions take precedence.',
    'language.label': 'Language',
    'translation.unavailable': 'Translation unavailable — shown in English',
    // Decision_Inbox (Coordinator_Console) — Req 12.1–12.3.
    'inbox.title': 'Decision inbox',
    'inbox.empty': 'No decisions are waiting for you',
    'inbox.loading': 'Loading decisions…',
    'inbox.reason': 'Why you are being asked',
    'inbox.stakes': 'Stakes',
    'inbox.defaultAction': 'If no response',
    'inbox.timeRemaining': 'Time remaining',
    'inbox.deadlinePassed': 'Deadline passed — default action applies',
    'inbox.deliveryFailed': 'Notification delivery failed — respond here',
    'inbox.submitError': 'Response was not recorded — please try again',
    'inbox.submitting': 'Submitting…',
    'inbox.connectionStale': 'Live updates interrupted — reconnecting',
    // Open-incident list + shelter availability (Coordinator_Console) — Req 12.4.
    'incidents.title': 'Open incidents',
    'incidents.empty': 'No incidents are open',
    'incidents.loading': 'Loading incidents…',
    'incidents.connectionStale': 'Live updates interrupted — reconnecting',
    'incidents.severity.NORMAL': 'Normal',
    'incidents.severity.WATCH': 'Watch',
    'incidents.severity.WARNING': 'Warning',
    'incidents.severity.EVACUATE': 'Evacuate',
    'incidents.affectedAreas': 'Affected areas',
    'incidents.affectedAreas.none': 'No areas reported',
    'incidents.openRequests': 'Open requests',
    'incidents.assignedResponders': 'Assigned responders',
    'incidents.shelters.title': 'Shelter availability',
    'incidents.shelters.available': 'Unoccupied places',
    'incidents.shelters.none': 'No shelters listed',
    // Per-incident audit trail (Coordinator_Console) — Req 12.7.
    'audit.title': 'Incident audit trail',
    'audit.incident': 'Incident',
    'audit.empty': 'No audit entries have been recorded for this incident yet',
    'audit.loading': 'Loading audit trail…',
    'audit.connectionStale': 'Live updates interrupted — reconnecting',
    'audit.timestamp': 'Time',
    'audit.tool': 'Action',
    'audit.outcome': 'Outcome',
    'audit.approver': 'Approved by',
    'audit.approver.none': 'Automatic (no human approval)',
    'audit.inputs': 'Inputs',
    'audit.inputs.none': 'No inputs recorded',
    // Recent-runs cost/latency view (Coordinator_Console) — Req 1.7, 12.8.
    'runs.title': 'Recent runs',
    'runs.empty': 'No runs have been recorded yet',
    'runs.loading': 'Loading runs…',
    'runs.connectionStale': 'Live updates interrupted — reconnecting',
    'runs.runId': 'Run',
    'runs.triggerType': 'Trigger',
    'runs.triggerSource': 'Trigger source',
    'runs.startedAt': 'Started',
    'runs.terminalStatus': 'Status',
    'runs.status.inProgress': 'In progress',
    'runs.tokens': 'Tokens',
    'runs.tokens.in': 'in',
    'runs.tokens.out': 'out',
    'runs.latency': 'Latency',
    'runs.latency.unit': 'ms',
    'runs.model': 'Model',
    'runs.cost': 'Estimated cost',
    'runs.value.none': 'Not recorded',
    // Coordinator_Orchestrator conversational panel (Coordinator_Console) — Req 12.11.
    'chat.title': 'Ask the coordinator assistant',
    'chat.intro':
      'Ask about incidents, requests, responders, or community safety guidance. This assistant can read and summarise but makes no changes.',
    'chat.transcript': 'Conversation',
    'chat.empty': 'No messages yet — ask a question to begin',
    'chat.inputLabel': 'Your message',
    'chat.placeholder': 'Type a question…',
    'chat.send': 'Send',
    'chat.sending': 'Sending…',
    'chat.speaker.you': 'You',
    'chat.speaker.orchestrator': 'Assistant',
    'chat.error': 'The assistant is unavailable right now — please try again',
    // Responder_Interface — Req 13.1–13.10.
    'responder.title': 'Your assignment',
    'responder.loading': 'Loading your assignment…',
    'responder.empty': 'You have no current assignment',
    'responder.unauthorised': 'Responder authorisation is required to view assignments.',
    'responder.connectionStale': 'Live updates interrupted — reconnecting',
    'responder.request': 'Request',
    'responder.state': 'Status',
    'responder.location': 'Location',
    'responder.occupants': 'Occupants',
    'responder.mobility': 'Mobility assistance needed',
    'responder.medical': 'Medical need',
    'responder.equipment': 'Equipment required',
    'responder.ackDeadline': 'Acknowledge by',
    'responder.yes': 'Yes',
    'responder.no': 'No',
    'responder.actions': 'Update your status',
    'responder.submitting': 'Submitting…',
    'responder.submitError': 'Status was not recorded — please try again',
    'responder.invalidTransition': 'That update is not allowed now. Allowed responses:',
    'responder.noneAllowed': 'none',
    'responder.action.accept': 'Accept',
    'responder.action.decline': 'Decline',
    'responder.action.en-route': 'On my way',
    'responder.action.on-scene': 'Arrived',
    'responder.action.completed': 'Rescue complete',
    'responder.state.AWAITING_ACK': 'Awaiting your acknowledgement',
    'responder.state.ACCEPTED': 'Accepted',
    'responder.state.EN_ROUTE': 'On my way',
    'responder.state.ON_SCENE': 'Arrived on scene',
    'responder.state.VERIFICATION': 'Rescue complete — awaiting coordinator verification',
    'responder.state.DECLINED': 'Declined — reassigning',
  },
  ta: {
    'status.title': 'சமூக வெள்ள நிலை',
    'status.severity.label': 'தற்போதைய நிலை',
    'status.severity.NORMAL': 'இயல்பு',
    'status.severity.WATCH': 'கண்காணிப்பு',
    'status.severity.WARNING': 'எச்சரிக்கை',
    'status.severity.EVACUATE': 'வெளியேறவும்',
    'status.severity.action.NORMAL': 'நடவடிக்கை தேவையில்லை. தகவலறிந்திருங்கள்.',
    'status.severity.action.WATCH': 'விழிப்புடன் இருந்து புதுப்பிப்புகளைக் கவனியுங்கள்.',
    'status.severity.action.WARNING': 'நகர தயாராகுங்கள். வெளியேற்றப் பொருட்களைத் தயாராக வைக்கவும்.',
    'status.severity.action.EVACUATE': 'இப்போதே வெளியேறி பட்டியலிடப்பட்ட தங்குமிடத்திற்குச் செல்லவும்.',
    'status.affectedAreas': 'பாதிக்கப்பட்ட பகுதிகள்',
    'status.affectedAreas.none': 'தற்போது பாதிக்கப்பட்ட பகுதிகள் எதுவும் தெரிவிக்கப்படவில்லை',
    'status.affectedAreas.requestCount': 'தெரிவிக்கப்பட்ட கோரிக்கைகள்',
    'status.recommendedAction': 'பரிந்துரைக்கப்பட்ட நடவடிக்கை',
    'status.readingTime': 'அளவீட்டு நேரம்',
    'status.readingStale': 'இந்த அளவீடு தற்போதையது அல்ல',
    'status.readingAge': 'அளவீட்டு வயது',
    'status.dataMayBeOutdated': 'இந்த மதிப்புகள் காலாவதியாகி இருக்கலாம்',
    'status.lastRetrieved': 'கடைசியாகப் பெறப்பட்டது',
    'shelters.title': 'தங்குமிடங்கள்',
    'shelters.name': 'தங்குமிடம்',
    'shelters.location': 'இடம்',
    'shelters.available': 'கிடைக்கும் இடங்கள்',
    'shelters.full': 'நிரம்பியது',
    'shelters.none': 'தங்குமிடங்கள் எதுவும் பட்டியலிடப்படவில்லை',
    'ruleset.title': 'செயலில் உள்ள விதி தொகுப்பு',
    'ruleset.threshold': 'வரம்பு',
    'ruleset.value': 'மதிப்பு',
    'ruleset.stalenessLimit': 'காலாவதி வரம்பு',
    'ruleset.version': 'விதி தொகுப்பு பதிப்பு',
    'advisory.title': 'ஆலோசனை',
    'advisory.notice':
      'ThunAI ஒரு சமூக ஒருங்கிணைப்பு உதவி, அதிகாரப்பூர்வ அவசர சேவை அல்ல. அதிகாரப்பூர்வ அவசர வழிமுறைகளே முதன்மையானவை.',
    'language.label': 'மொழி',
    // 'translation.unavailable' intentionally omitted → falls back to English.
    // Decision_Inbox (Coordinator_Console) — Req 12.1–12.3.
    'inbox.title': 'முடிவு உள்பெட்டி',
    'inbox.empty': 'உங்களுக்காக முடிவுகள் எதுவும் காத்திருக்கவில்லை',
    'inbox.loading': 'முடிவுகள் ஏற்றப்படுகின்றன…',
    'inbox.reason': 'நீங்கள் ஏன் கேட்கப்படுகிறீர்கள்',
    'inbox.stakes': 'பங்குகள்',
    'inbox.defaultAction': 'பதில் இல்லையெனில்',
    'inbox.timeRemaining': 'மீதமுள்ள நேரம்',
    'inbox.deadlinePassed': 'காலக்கெடு முடிந்தது — இயல்புநிலை நடவடிக்கை பொருந்தும்',
    'inbox.deliveryFailed': 'அறிவிப்பு வழங்கல் தோல்வியடைந்தது — இங்கே பதிலளிக்கவும்',
    'inbox.submitError': 'பதில் பதிவு செய்யப்படவில்லை — மீண்டும் முயற்சிக்கவும்',
    'inbox.submitting': 'சமர்ப்பிக்கப்படுகிறது…',
    'inbox.connectionStale': 'நேரடி புதுப்பிப்புகள் தடைபட்டன — மீண்டும் இணைக்கிறது',
    // Open-incident list + shelter availability (Coordinator_Console) — Req 12.4.
    'incidents.title': 'திறந்த சம்பவங்கள்',
    'incidents.empty': 'திறந்த சம்பவங்கள் எதுவும் இல்லை',
    'incidents.loading': 'சம்பவங்கள் ஏற்றப்படுகின்றன…',
    'incidents.connectionStale': 'நேரடி புதுப்பிப்புகள் தடைபட்டன — மீண்டும் இணைக்கிறது',
    'incidents.severity.NORMAL': 'இயல்பு',
    'incidents.severity.WATCH': 'கண்காணிப்பு',
    'incidents.severity.WARNING': 'எச்சரிக்கை',
    'incidents.severity.EVACUATE': 'வெளியேறவும்',
    'incidents.affectedAreas': 'பாதிக்கப்பட்ட பகுதிகள்',
    'incidents.affectedAreas.none': 'பகுதிகள் எதுவும் தெரிவிக்கப்படவில்லை',
    'incidents.openRequests': 'திறந்த கோரிக்கைகள்',
    'incidents.assignedResponders': 'நியமிக்கப்பட்ட பதிலளிப்பாளர்கள்',
    'incidents.shelters.title': 'தங்குமிட இருப்பு',
    'incidents.shelters.available': 'காலியான இடங்கள்',
    'incidents.shelters.none': 'தங்குமிடங்கள் எதுவும் பட்டியலிடப்படவில்லை',
    // Per-incident audit trail (Coordinator_Console) — Req 12.7.
    'audit.title': 'சம்பவத் தணிக்கை பதிவு',
    'audit.incident': 'சம்பவம்',
    'audit.empty': 'இந்தச் சம்பவத்திற்கு இதுவரை தணிக்கை பதிவுகள் எதுவும் பதிவாகவில்லை',
    'audit.loading': 'தணிக்கை பதிவு ஏற்றப்படுகிறது…',
    'audit.connectionStale': 'நேரடி புதுப்பிப்புகள் தடைபட்டன — மீண்டும் இணைக்கிறது',
    'audit.timestamp': 'நேரம்',
    'audit.tool': 'செயல்',
    'audit.outcome': 'விளைவு',
    'audit.approver': 'அனுமதித்தவர்',
    'audit.approver.none': 'தானியங்கி (மனித அனுமதி இல்லை)',
    'audit.inputs': 'உள்ளீடுகள்',
    'audit.inputs.none': 'உள்ளீடுகள் எதுவும் பதிவாகவில்லை',
    // Recent-runs cost/latency view (Coordinator_Console) — Req 1.7, 12.8.
    'runs.title': 'சமீபத்திய இயக்கங்கள்',
    'runs.empty': 'இதுவரை இயக்கங்கள் எதுவும் பதிவாகவில்லை',
    'runs.loading': 'இயக்கங்கள் ஏற்றப்படுகின்றன…',
    'runs.connectionStale': 'நேரடி புதுப்பிப்புகள் தடைபட்டன — மீண்டும் இணைக்கிறது',
    'runs.runId': 'இயக்கம்',
    'runs.triggerType': 'தூண்டுதல்',
    'runs.triggerSource': 'தூண்டுதல் மூலம்',
    'runs.startedAt': 'தொடங்கியது',
    'runs.terminalStatus': 'நிலை',
    'runs.status.inProgress': 'நடந்து கொண்டிருக்கிறது',
    'runs.tokens': 'டோக்கன்கள்',
    'runs.tokens.in': 'உள்',
    'runs.tokens.out': 'வெளி',
    'runs.latency': 'தாமதம்',
    'runs.latency.unit': 'மி.வி',
    'runs.model': 'மாதிரி',
    'runs.cost': 'மதிப்பிடப்பட்ட செலவு',
    'runs.value.none': 'பதிவு செய்யப்படவில்லை',
    // Coordinator_Orchestrator conversational panel (Coordinator_Console) — Req 12.11.
    'chat.title': 'ஒருங்கிணைப்பாளர் உதவியாளரிடம் கேளுங்கள்',
    'chat.intro':
      'சம்பவங்கள், கோரிக்கைகள், பதிலளிப்பாளர்கள் அல்லது சமூகப் பாதுகாப்பு வழிகாட்டுதல் பற்றிக் கேளுங்கள். இந்த உதவியாளர் படித்துச் சுருக்கமளிக்கும், ஆனால் எந்த மாற்றத்தையும் செய்யாது.',
    'chat.transcript': 'உரையாடல்',
    'chat.empty': 'இதுவரை செய்திகள் இல்லை — தொடங்க ஒரு கேள்வியைக் கேளுங்கள்',
    'chat.inputLabel': 'உங்கள் செய்தி',
    'chat.placeholder': 'ஒரு கேள்வியைத் தட்டச்சு செய்யுங்கள்…',
    'chat.send': 'அனுப்பு',
    'chat.sending': 'அனுப்பப்படுகிறது…',
    'chat.speaker.you': 'நீங்கள்',
    'chat.speaker.orchestrator': 'உதவியாளர்',
    'chat.error': 'உதவியாளர் தற்போது கிடைக்கவில்லை — மீண்டும் முயற்சிக்கவும்',
    // Responder_Interface — Req 13.1–13.10.
    'responder.title': 'உங்கள் பணி',
    'responder.loading': 'உங்கள் பணி ஏற்றப்படுகிறது…',
    'responder.empty': 'உங்களுக்கு தற்போது பணி எதுவும் இல்லை',
    'responder.unauthorised': 'பணிகளைப் பார்க்க பதிலளிப்பாளர் அனுமதி தேவை.',
    'responder.connectionStale': 'நேரடி புதுப்பிப்புகள் தடைபட்டன — மீண்டும் இணைக்கிறது',
    'responder.request': 'கோரிக்கை',
    'responder.state': 'நிலை',
    'responder.location': 'இடம்',
    'responder.occupants': 'குடியிருப்பாளர்கள்',
    'responder.mobility': 'நடமாட்டஉதவி தேவை',
    'responder.medical': 'மருத்துவத் தேவை',
    'responder.equipment': 'தேவையான உபகரணம்',
    'responder.ackDeadline': 'இதற்குள் ஒப்புக்கொள்ளவும்',
    'responder.yes': 'ஆம்',
    'responder.no': 'இல்லை',
    'responder.actions': 'உங்கள் நிலையைப் புதுப்பிக்கவும்',
    'responder.submitting': 'சமர்ப்பிக்கப்படுகிறது…',
    'responder.submitError': 'நிலை பதிவு செய்யப்படவில்லை — மீண்டும் முயற்சிக்கவும்',
    'responder.invalidTransition': 'இந்தப் புதுப்பிப்பு இப்போது அனுமதிக்கப்படவில்லை. அனுமதிக்கப்பட்ட பதில்கள்:',
    'responder.noneAllowed': 'எதுவும் இல்லை',
    'responder.action.accept': 'ஏற்றுக்கொள்',
    'responder.action.decline': 'நிராகரி',
    'responder.action.en-route': 'நான் வழியில்',
    'responder.action.on-scene': 'வந்துவிட்டேன்',
    'responder.action.completed': 'மீட்பு முடிந்தது',
    'responder.state.AWAITING_ACK': 'உங்கள் ஒப்புதலுக்காகக் காத்திருக்கிறது',
    'responder.state.ACCEPTED': 'ஏற்கப்பட்டது',
    'responder.state.EN_ROUTE': 'வழியில்',
    'responder.state.ON_SCENE': 'இடத்தில்',
    'responder.state.VERIFICATION': 'முடிந்தது — ஒருங்கிணைப்பாளர் சரிபார்ப்புக்காகக் காத்திருக்கிறது',
    'responder.state.DECLINED': 'நிராகரிக்கப்பட்டது — மீண்டும் ஒதுக்கப்படுகிறது',
  },
};

/** Resolution of a single key, carrying whether a fallback was used (Req 14.10). */
export interface Translation {
  /** The resolved text (selected language, or default-language fallback). */
  value: string;
  /** True when the selected language lacked the key and the default was used. */
  fallback: boolean;
  /** The language the returned `value` is actually in. */
  resolvedLanguage: LanguageCode;
}

/**
 * Resolve a key in the given language. On a missing key, fall back to the
 * default language and mark `fallback = true`; if the key is missing there too,
 * return the key itself so the UI never renders `undefined`.
 */
export function translate(key: TranslationKey, language: LanguageCode): Translation {
  const selected = DICTIONARIES[language]?.[key];
  if (selected !== undefined) {
    return { value: selected, fallback: false, resolvedLanguage: language };
  }

  const fallbackValue = DICTIONARIES[DEFAULT_LANGUAGE]?.[key];
  if (fallbackValue !== undefined) {
    return { value: fallbackValue, fallback: true, resolvedLanguage: DEFAULT_LANGUAGE };
  }

  return { value: key, fallback: true, resolvedLanguage: DEFAULT_LANGUAGE };
}

export interface I18n {
  /** Currently selected language. */
  language: LanguageCode;
  /** Switch language; applies to every subsequently resolved field (Req 14.4). */
  setLanguage: (language: LanguageCode) => void;
  /** Resolve a key to plain text (no fallback signal). Convenience wrapper. */
  t: (key: TranslationKey) => string;
  /** Resolve a key with the per-field fallback signal (Req 14.10). */
  tf: (key: TranslationKey) => Translation;
}

/**
 * React hook exposing the current language, a setter, and resolvers. Defaults
 * to the configured default language on initial load (Req 14.4).
 */
export function useI18n(initial: LanguageCode = DEFAULT_LANGUAGE): I18n {
  const [language, setLanguage] = useState<LanguageCode>(initial);

  const tf = useCallback((key: TranslationKey) => translate(key, language), [language]);
  const t = useCallback((key: TranslationKey) => translate(key, language).value, [language]);

  return { language, setLanguage, t, tf };
}
