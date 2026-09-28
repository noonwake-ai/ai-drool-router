// SPDX-FileCopyrightText: 2026 NoonWake.AI
// SPDX-License-Identifier: LGPL-3.0-or-later

import {tr} from './i18n-core.js';
import {runUrl} from './endpoints.js';

export async function readRun(id, signal, fetcher = fetch) {
  const response = await fetcher(runUrl(id), {method:'GET', signal, cache:'no-store'});
  if (!response.ok) throw Error(response.status === 410 ? tr('error.runExpired') : tr('error.runLoad'));
  const detail = await response.json();
  if (detail?.id !== id) throw Error(tr('error.runMismatch'));
  return detail;
}
