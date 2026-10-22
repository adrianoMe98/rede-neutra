document.addEventListener('DOMContentLoaded', () => {
  const provider = document.getElementById('id_provider');
  const subscriber = document.getElementById('id_subscriber');
  const preview = document.getElementById('description-preview');
  const vlan = document.getElementById('id_vlan');
  const data = document.getElementById('vlans-data');
  const profilesNode = document.getElementById('profiles-data');
  const snapshotNode = document.getElementById('profile-snapshot');
  const profiles = profilesNode ? JSON.parse(profilesNode.textContent) : [];
  const snapshot = snapshotNode ? JSON.parse(snapshotNode.textContent) : {};
  const profile = document.getElementById('id_profile');
  const olt = document.getElementById('id_olt');
  const profilePreview = document.getElementById('profile-preview');
  const lockedOlt = document.querySelector('[data-discovered-olt]')?.dataset.discoveredOlt;
  const update = () => {
    if (provider && subscriber && preview) {
      const label = provider.value ? provider.options[provider.selectedIndex].text : '';
      preview.textContent = label && subscriber.value.trim() ? `${label} - ${subscriber.value.trim()}` : 'Selecione o provedor e informe o cliente';
    }
    if (provider && vlan && data) {
        const vlans = JSON.parse(data.textContent);
        Array.from(vlan.options).forEach(option => {
          const item = vlans.find(v => String(v.id) === option.value);
          option.hidden = !!item && !!provider.value && String(item.provider_id) !== provider.value;
          option.disabled = option.hidden;
          if (option.hidden && option.selected) vlan.value = '';
        });
    }
  };
  if (provider) {
    provider.addEventListener('change', update);
  }
  subscriber?.addEventListener('input', update);
  if (profile && olt) {
    if (lockedOlt) {
      for (const option of profile.options) {
        const item = profiles.find(p => String(p.id) === option.value);
        if (item && String(item.olt_id) !== lockedOlt) option.disabled = true;
      }
      for (const option of olt.options) option.disabled = option.value !== lockedOlt;
      for (const name of ['serial', 'frame', 'slot', 'pon']) {
        const field = document.getElementById(`id_${name}`);
        if (field) field.readOnly = true;
      }
    }
    const applyProfile = () => {
      const item = profiles.find(p => String(p.id) === profile.value);
      if (!item) { if (profilePreview) profilePreview.textContent = 'Sem perfil: utiliza os parâmetros padrão da OLT.'; return; }
      provider.value = String(item.provider_id);
      olt.value = String(item.olt_id);
      update();
      vlan.value = String(item.vlan_id);
      const params = snapshot && snapshot.profile_id === item.id ? snapshot : item;
      if (profilePreview) profilePreview.textContent = `Line profile ${params.line_profile} · Service profile ${params.service_profile} · GEM ${params.gem_port} · VLAN cliente ${params.user_vlan ?? vlan.selectedOptions[0]?.text.split(' · ')[0]}`;
    };
    profile.addEventListener('change', applyProfile);
    applyProfile();
  }
  update();
  document.querySelectorAll('.provision-form, .panel-action, .discovery-form').forEach(form => {
    form.addEventListener('submit', () => {
      const button = form.querySelector('button[type="submit"]');
      if (button) { button.disabled = true; button.textContent = 'Processando…'; }
    });
  });
});
