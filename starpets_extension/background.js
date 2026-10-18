// Кнопка расширения: на сайте StarPets показывает/скрывает панель, на других сайтах открывает StarPets.
chrome.action.onClicked.addListener((tab) => {
  if (!tab || tab.id === undefined) return;
  chrome.tabs.sendMessage(tab.id, { spx: 'toggle' }).catch(() => chrome.tabs.create({ url: 'https://starpets.gg/' }));
});
