/* Стандартные ответы: модальный редактор, открывается из «Звукового кеша»
   (window.openReplyEditor; после сохранения — событие replies-changed). */
(() => {
  const CASES = { unclaimed: 'Устройство не привязано к пользователю' };

  function normalizeText(s) {
    return s.split(/\s+/).join(' ').trim();
  }

  window.openReplyEditor = async (match) => {
    // Load fresh settings
    const cfg = await api('settings');
    const replies = cfg.standard_replies || [];

    // Find existing reply if editing
    let reply = null;
    let isNew = true;
    if (match) {
      if (match.name) {
        reply = replies.find(r => r.name === match.name);
      }
      if (!reply && match.text) {
        const norm = normalizeText(match.text);
        reply = replies.find(r => normalizeText(r.text) === norm);
      }
      if (reply) isNew = false;
    }

    // Build dialog
    const dlg = document.createElement('dialog');
    dlg.className = 'catalog-dialog reply-dialog';
    dlg.innerHTML = `
      <form method="dialog">
        <h2>${isNew ? 'Новый стандартный ответ' : 'Стандартный ответ'}</h2>
        <p class="error" style="display:none"></p>
        <label>Название<input type="text" name="name" required></label>
        <label>Случай
          <select name="case">
            ${Object.entries(CASES).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}
          </select>
        </label>
        <label class="check"><input type="checkbox" name="enabled"> Включено</label>
        <label>Текст ответа
          <textarea name="text" rows="5" maxlength="2000"></textarea>
          <span class="hint counter">0 / 2000</span>
        </label>
        <div class="row">
          ${!isNew ? '<button type="button" class="danger" name="delete">Удалить</button>' : ''}
          <button type="button" class="secondary" name="cancel">Отмена</button>
          <button type="submit" class="primary">Сохранить</button>
        </div>
      </form>
    `;

    // Populate fields
    const form = dlg.querySelector('form');
    const f = name => form.elements.namedItem(name); // form.name — атрибут формы, поля только через elements
    f('name').value = reply?.name || '';
    f('case').value = reply?.case || 'unclaimed';
    f('enabled').checked = reply?.enabled ?? true;
    f('text').value = reply?.text || '';
    const counter = () => { form.querySelector('.hint').textContent = `${f('text').value.length} / 2000` };
    f('text').addEventListener('input', counter);
    counter();

    // Error display
    const errorEl = dlg.querySelector('.error');
    function showError(msg) {
      errorEl.textContent = msg;
      errorEl.style.display = 'block';
    }
    function hideError() {
      errorEl.style.display = 'none';
    }

    // Disable/enable buttons
    const buttons = dlg.querySelectorAll('button');
    function setSaving(saving) {
      buttons.forEach(b => b.disabled = saving);
    }

    // Handle save
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      hideError();
      const text = f('text').value.trim();
      if (!text) {
        showError('Текст ответа не может быть пустым');
        return;
      }
      setSaving(true);
      try {
        // Fetch fresh settings again before saving
        const freshCfg = await api('settings');
        let freshReplies = freshCfg.standard_replies || [];

        const newReply = {
          id: reply?.id || crypto.randomUUID(),
          name: f('name').value.trim(),
          enabled: f('enabled').checked,
          case: f('case').value,
          text: text
        };

        if (isNew) {
          freshReplies.push(newReply);
        } else {
          freshReplies = freshReplies.map(r => r.id === reply.id ? newReply : r);
        }

        freshCfg.standard_replies = freshReplies;
        await api('settings', 'PUT', freshCfg);
        dlg.close();
        message('Стандартный ответ сохранён');
        window.dispatchEvent(new Event('replies-changed'));
      } catch (err) {
        showError(err.message || 'Ошибка сохранения');
      } finally {
        setSaving(false);
      }
    });

    // Handle delete
    const deleteBtn = dlg.querySelector('[name="delete"]');
    if (deleteBtn) {
      deleteBtn.addEventListener('click', async () => {
        if (!confirm(`Удалить стандартный ответ «${reply.name}»?`)) return;
        hideError();
        setSaving(true);
        try {
          const freshCfg = await api('settings');
          let freshReplies = freshCfg.standard_replies || [];
          freshReplies = freshReplies.filter(r => r.id !== reply.id);
          freshCfg.standard_replies = freshReplies;
          await api('settings', 'PUT', freshCfg);
          dlg.close();
          message('Стандартный ответ удалён');
          window.dispatchEvent(new Event('replies-changed'));
        } catch (err) {
          showError(err.message || 'Ошибка удаления');
        } finally {
          setSaving(false);
        }
      });
    }

    // Cancel button
    dlg.querySelector('[name="cancel"]').addEventListener('click', () => dlg.close());

    // Cleanup on close
    dlg.addEventListener('close', () => dlg.remove());

    document.body.append(dlg);
    dlg.showModal();
    f('text').focus();
  };
})();