document.addEventListener('DOMContentLoaded', () => {
  const provider = document.getElementById('id_provider');
  const subscriber = document.getElementById('id_subscriber');
  const preview = document.getElementById('description-preview');
  const vlan = document.getElementById('id_vlan');
  const data = document.getElementById('vlans-data');
  if (provider && subscriber && preview) {
    const update = () => {
      const label = provider.value ? provider.options[provider.selectedIndex].text : '';
      preview.textContent = label && subscriber.value.trim() ? `${label} - ${subscriber.value.trim()}` : 'Selecione o provedor e informe o cliente';
      if (vlan && data) {
        const vlans = JSON.parse(data.textContent);
        Array.from(vlan.options).forEach(option => {
          const item = vlans.find(v => String(v.id) === option.value);
          option.hidden = !!item && !!provider.value && String(item.provider_id) !== provider.value;
          option.disabled = option.hidden;
          if (option.hidden && option.selected) vlan.value = '';
        });
      }
    };
    provider.addEventListener('change', update);
    subscriber.addEventListener('input', update);
    update();
  }
  document.querySelectorAll('.provision-form, .panel-action').forEach(form => {
    form.addEventListener('submit', () => {
      const button = form.querySelector('button[type="submit"]');
      if (button) { button.disabled = true; button.textContent = 'Processando…'; }
    });
  });
});
