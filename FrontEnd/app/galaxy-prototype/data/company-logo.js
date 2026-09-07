export function tickerCode(value = '') {
  return String(value).match(/\d{6}/)?.[0] ?? '';
}

export function tossSecuritiesLogoUrl(company) {
  const ticker = String(company?.ticker ?? company?.id ?? '').trim().toUpperCase();
  const marketTicker = ticker.split('.')[0];
  const symbol = tickerCode(ticker) || marketTicker.replace(/[^A-Z0-9]/g, '');
  if (!symbol || symbol === 'NEWS') return null;
  return `https://static.toss.im/png-icons/securities/icn-sec-fill-${symbol}.png`;
}
