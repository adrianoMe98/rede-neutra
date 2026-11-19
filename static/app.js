document.addEventListener('DOMContentLoaded', () => {
  const provider = document.getElementById('id_provider');
  const subscriber = document.getElementById('id_subscriber');
  const preview = document.getElementById('description-preview');
  const vlan = document.getElementById('id_vlan');
  const data = document.getElementById('vlans-data');
  const choicesNode = document.getElementById('profile-vlan-choices');
  const profilesNode = document.getElementById('profiles-data');
  const snapshotNode = document.getElementById('profile-snapshot');
  const vlans = new Map((data ? JSON.parse(data.textContent) : []).map(item => [String(item.id), item]));
  const choices = choicesNode ? JSON.parse(choicesNode.textContent) : {};
  const profiles = new Map((profilesNode ? JSON.parse(profilesNode.textContent) : []).map(item => [String(item.id), item]));
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
    const selectedProfile = profiles.get(profile?.value);
    if (provider && vlan && (data || choicesNode)) {
      for (const option of vlan.options) {
        if (!option.value) continue;
        const item = vlans.get(option.value);
        const providerId = item?.provider_id ?? choices[option.value];
        const wrongProvider = (choicesNode || provider.value) && String(providerId) !== provider.value;
        const wrongGroup = selectedProfile?.vlan_description && (
          item?.name !== selectedProfile.vlan_description || item?.provider_id !== selectedProfile.provider_id
        );
        const wrongFixedVlan = selectedProfile?.vlan_id && String(selectedProfile.vlan_id) !== option.value;
        option.hidden = option.disabled = Boolean(wrongProvider || wrongGroup || wrongFixedVlan);
      }
      if (vlan.selectedOptions[0]?.disabled) vlan.value = '';
    }
    if (profilePreview) {
      if (!selectedProfile) {
        profilePreview.textContent = 'Sem perfil. Utiliza os parâmetros padrão da OLT.';
        return;
      }
      const params = snapshot?.profile_id === selectedProfile.id ? snapshot : selectedProfile;
      const serviceVlan = vlans.get(vlan.value)?.number;
      const userVlan = params.user_vlan_follows_service ? serviceVlan : (params.user_vlan ?? serviceVlan);
      profilePreview.textContent = `Line profile ${params.line_profile} · Service profile ${params.service_profile} · GEM ${params.gem_port} · VLAN cliente ${userVlan ?? '—'}`;
    }
  };
  if (provider) {
    provider.addEventListener('change', update);
  }
  subscriber?.addEventListener('input', update);
  vlan?.addEventListener('change', update);
  if (profile && olt) {
    if (lockedOlt) {
      for (const option of profile.options) {
        const item = profiles.get(option.value);
        if (item && String(item.olt_id) !== lockedOlt) option.disabled = true;
      }
      for (const option of olt.options) option.disabled = option.value !== lockedOlt;
      for (const name of ['serial', 'frame', 'slot', 'pon']) {
        const field = document.getElementById(`id_${name}`);
        if (field) field.readOnly = true;
      }
    }
    const applyProfile = () => {
      const item = profiles.get(profile.value);
      if (item) {
        provider.value = String(item.provider_id);
        olt.value = String(item.olt_id);
        if (item.vlan_id) vlan.value = String(item.vlan_id);
      }
      update();
    };
    profile.addEventListener('change', applyProfile);
    applyProfile();
  } else {
    update();
  }
  document.querySelectorAll('.provision-form, .panel-action, .discovery-form, .communication-form').forEach(form => {
    form.addEventListener('submit', event => {
      // Preservar a ação escolhida: botões desabilitados não entram no POST.
      const clicked = event.submitter;
      if (clicked?.name) {
        const action = document.createElement('input');
        action.type = 'hidden'; action.name = clicked.name; action.value = clicked.value;
        form.appendChild(action);
      }
      form.querySelectorAll('button[type="submit"]').forEach(button => { button.disabled = true; });
      if (clicked) clicked.textContent = 'Processando…';
    });
  });
});
