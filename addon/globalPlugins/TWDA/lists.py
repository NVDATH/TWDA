# -*- coding: utf-8 -*-
# tweetAccess - lists.py: the Lists tab, after NVSky's Lists window. A tree of the lists of the account and, beside it,
# the tweets of the one selected, in the same kind of list as every other tab of tweets (ListFeed). Nothing is asked
# of X while arrowing through the tree: the tweets shown come from the cache, Enter loads a list that was never read
# and F5 reads it again. The menu of a list (Applications key, Shift+F10 or a right click on the tree) and the keys of
# the Lists area of the Shortcuts page do what Alt+A cannot: Alt+A stays the menu of a tweet.

import json
import time  # TEMP
import webbrowser

import addonHandler
import ui
import wx
from logHandler import log

import gui.nvdaControls

from . import keymap, soundpack, tabstate
from .newchat import user_label
from .tabs import FeedTab
from .utils import TAG, announce_now, safe_ui

addonHandler.initTranslation()

CACHE_KEY = "lists_cache"  # account meta: the rows of the lists of the account (see service._list_row)
STATE_KEY = "lists_state"  # account meta: the list shown last
STATE_SAVE_DELAY_MS = 800
LIST_URL = "https://x.com/i/lists/"


def list_label(row):
	"""The label of a list in the tree: its name, then whether it is followed (not the account's own), private or
	public, and how many members it has."""
	tags = []
	if not row.get("owned", True):
		# Translators: Tag of a list that the account follows but did not create, in the list of lists.
		tags.append(_("followed"))
	if row.get("private"):
		# Translators: Tag of a private list, in the list of lists.
		tags.append(_("private"))
	else:
		# Translators: Tag of a public list, in the list of lists.
		tags.append(_("public"))
	# Translators: Tag of a list with its number of members, in the list of lists. {count} is the number.
	tags.append(_("{count} members").format(count=row.get("members") or 0))
	# Translators: Label of a list in the tree of lists. {name} is the name of the list, {tags} a comma separated list such as "private, 7 members".
	return _("{name} ({tags})").format(name=row.get("name") or "", tags=", ".join(tags))


class ListDialog(wx.Dialog):
	"""The window that creates a list or edits one (row: the list as the tab has it): its name, its description and
	whether it is private. values() gives (name, description, private) after OK."""

	def __init__(self, parent, row=None):
		if row is None:
			# Translators: Title of the window that creates a list.
			title = _("New list")
		else:
			# Translators: Title of the window that edits a list.
			title = _("Edit list")
		super().__init__(parent, title=title)
		row = row or {}
		sizer = wx.BoxSizer(wx.VERTICAL)
		# Translators: Label of the name field of the list window.
		sizer.Add(wx.StaticText(self, label=_("&Name:")), 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
		self.nameBox = wx.TextCtrl(self, value=row.get("name", ""), size=(380, -1))
		sizer.Add(self.nameBox, 0, wx.EXPAND | wx.ALL, 10)
		# Translators: Label of the description field of the list window.
		sizer.Add(wx.StaticText(self, label=_("&Description (optional):")), 0, wx.LEFT | wx.RIGHT, 10)
		self.descriptionBox = wx.TextCtrl(self, value=row.get("description", ""), style=wx.TE_MULTILINE, size=(380, 80))
		sizer.Add(self.descriptionBox, 0, wx.EXPAND | wx.ALL, 10)
		# Translators: Checkbox of the list window: a private list is seen only by its owner.
		self.privateBox = wx.CheckBox(self, label=_("&Private list"))
		self.privateBox.SetValue(bool(row.get("private")))
		sizer.Add(self.privateBox, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
		buttons = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Buttons of the list window.
		self.okButton = wx.Button(self, wx.ID_OK, _("OK"))
		self.cancelButton = wx.Button(self, wx.ID_CANCEL, _("Cancel"))
		self.okButton.SetDefault()
		buttons.Add(self.okButton, 0, wx.RIGHT, 8)
		buttons.Add(self.cancelButton, 0)
		sizer.Add(buttons, 0, wx.ALIGN_CENTER | wx.BOTTOM, 10)
		self.SetSizerAndFit(sizer)
		self.SetEscapeId(wx.ID_CANCEL)
		self.CentreOnParent()
		self.okButton.Bind(wx.EVT_BUTTON, self.on_ok)
		self.nameBox.SetFocus()

	def on_ok(self, event):
		if not self.nameBox.GetValue().strip():
			# Translators: Announced when a list is saved with no name typed.
			ui.message(_("A list needs a name."))
			self.nameBox.SetFocus()
			return
		self.EndModal(wx.ID_OK)

	def values(self):
		return self.nameBox.GetValue().strip(), self.descriptionBox.GetValue().strip(), self.privateBox.GetValue()


def member_names(users):
	"""'@a, @b' of some users, for what is announced."""
	return ", ".join("@" + u["handle"] for u in users)


class AddPeopleDialog(wx.Dialog):
	"""The window that finds users to add to a list, after the New chat window: search for a user (only when the reader
	asks: Enter or Find), check the ones to add. self.users (dicts with id, handle, name, bio) after OK."""

	def __init__(self, parent, plugin, taken):
		# Translators: Title of the window that adds people to a list.
		super().__init__(parent, title=_("Add people to the list"), size=(480, 440))
		self._plugin = plugin
		self._taken = set(taken)  # ids of the users the list has already
		self._dead = False
		self._found = []
		self.users = []

		sizer = wx.BoxSizer(wx.VERTICAL)
		row = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Label of the box that searches for a user in the window that adds people to a list.
		row.Add(wx.StaticText(self, label=_("Search for a &user:")), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
		self.query = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
		row.Add(self.query, 1)
		# Translators: Button of the window that adds people to a list: searches for the user typed.
		self.findButton = wx.Button(self, label=_("&Find"))
		row.Add(self.findButton, 0, wx.LEFT, 5)
		sizer.Add(row, 0, wx.EXPAND | wx.ALL, 8)
		# Translators: Label of the list of the users found, in the window that adds people to a list.
		self.foundLabel = wx.StaticText(self, label=_("Search &results (check the people to add):"))
		self.found = gui.nvdaControls.CustomCheckListBox(self, choices=[], size=(-1, 200))
		sizer.Add(self.foundLabel, 0, wx.LEFT | wx.RIGHT, 8)
		sizer.Add(self.found, 1, wx.EXPAND | wx.ALL, 8)
		buttons = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Button of the window that adds people to a list: adds the checked users.
		self.addButton = wx.Button(self, wx.ID_OK, _("&Add checked"))
		self.cancelButton = wx.Button(self, wx.ID_CANCEL, _("&Cancel"))
		buttons.Add(self.addButton, 0, wx.RIGHT, 8)
		buttons.Add(self.cancelButton, 0)
		sizer.Add(buttons, 0, wx.ALIGN_CENTER | wx.BOTTOM, 8)
		self.SetSizer(sizer)
		self.SetEscapeId(wx.ID_CANCEL)
		self.CentreOnParent()
		self.query.Bind(wx.EVT_TEXT_ENTER, self.on_find)
		self.findButton.Bind(wx.EVT_BUTTON, self.on_find)
		self.addButton.Bind(wx.EVT_BUTTON, self.on_add)
		self._show_found(False)
		self.query.SetFocus()

	def Destroy(self):
		self._dead = True  # a result that arrives later is thrown away (see safe_ui)
		return super().Destroy()

	def _show_found(self, shown):
		for control in (self.foundLabel, self.found, self.addButton):
			control.Show(shown)
			control.Enable(shown)
		self.Layout()

	def on_find(self, event=None):
		query = self.query.GetValue().strip()
		if not query:
			ui.message(_("Type something to search for."))
			self.query.SetFocus()
			return
		options = {
			"kind": "search", "params": {"handle": query, "query": query}, "chain": "listadd:%s" % query, "pages": 1,
		}
		if not self._plugin.start_job("users", self.on_found, options):
			ui.message(_("Still loading, please wait."))
			return
		ui.message(_("Loading, please wait."))

	@safe_ui
	def on_found(self, result):
		if not result["ok"]:
			soundpack.play("error")
			ui.message(result["message"])
			return
		self._found = [u for u in result["users"] if u["id"] not in self._taken]
		if not self._found:
			self._show_found(False)
			ui.message(_("No users found."))
			self.query.SetFocus()
			return
		self.found.Set([user_label(u) for u in self._found])
		self.found.CheckedItems = []
		self._show_found(True)
		self.found.SetSelection(0)
		self.found.SetFocus()  # the first of them, so the arrow keys and Space work at once

	def on_add(self, event=None):
		if not self.addButton.IsShown():
			return
		picked = list(self.found.CheckedItems)
		if not picked and self.found.GetSelection() != wx.NOT_FOUND:
			picked = [self.found.GetSelection()]  # nothing checked: the highlighted one
		users = [self._found[i] for i in picked if 0 <= i < len(self._found)]
		if not users:
			# Translators: Announced when Add checked is pressed with no user to add, in the window that adds people to a list.
			announce_now(_("No users checked."))
			return
		self.users = users
		self.EndModal(wx.ID_OK)


class ManageMembersDialog(wx.Dialog):
	"""The members of a list: who they are, taking some out, adding people. Read only when the account did not create
	the list (X lets only its owner change who is in it). delta: how many members the changes made so far added
	(negative: took out), so the tab can keep the count of the list."""

	def __init__(self, parent, plugin, row, members):
		# Translators: Title of the window that manages the members of a list. {name} is the name of the list.
		super().__init__(parent, title=_("Manage members - {name}").format(name=row["name"]), size=(480, 540))
		self._plugin = plugin
		self._row = row
		self._members = list(members)
		self._owner = bool(row.get("owned", True))
		self._dead = False
		self._busy = False  # a change is running
		self.delta = 0

		sizer = wx.BoxSizer(wx.VERTICAL)
		# Translators: Label of the list of the members of a list, in the window that manages them.
		sizer.Add(wx.StaticText(self, label=_("Current &members:")), 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
		if self._owner:
			self.memberList = gui.nvdaControls.CustomCheckListBox(self, choices=[])
		else:
			self.memberList = wx.ListBox(self, choices=[])
		sizer.Add(self.memberList, 1, wx.EXPAND | wx.ALL, 10)
		# An empty checklist shows one blank row that can be checked: a text takes its place.
		# Translators: Shown in place of the list of members when the list has none yet.
		self.emptyLabel = wx.StaticText(self, label=_("This list has no members."))
		sizer.Add(self.emptyLabel, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
		if self._owner:
			# Translators: Button of the window that manages the members of a list: takes the checked members out.
			self.removeButton = wx.Button(self, label=_("&Remove checked"))
			# Translators: Button of the window that manages the members of a list: opens the window that searches for people to add.
			self.addButton = wx.Button(self, label=_("&Add people..."))
			sizer.Add(self.removeButton, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
			sizer.Add(self.addButton, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
		else:
			# Translators: Note of the window that manages the members of a list when the account did not create the list.
			sizer.Add(wx.StaticText(self, label=_("You did not create this list: its members are read only.")), 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
		# Translators: Button that closes the window that manages the members of a list.
		self.closeButton = wx.Button(self, wx.ID_CANCEL, _("&Close"))
		sizer.Add(self.closeButton, 0, wx.ALIGN_CENTER | wx.BOTTOM, 10)
		self.SetSizer(sizer)
		self.SetEscapeId(wx.ID_CANCEL)
		self.CentreOnParent()
		if self._owner:
			self.removeButton.Bind(wx.EVT_BUTTON, self.on_remove)
			self.addButton.Bind(wx.EVT_BUTTON, self.on_add_people)
		self._render()
		(self.memberList if self._members else (self.addButton if self._owner else self.closeButton)).SetFocus()

	def Destroy(self):
		self._dead = True  # a result that arrives later is thrown away (see safe_ui)
		return super().Destroy()

	def _render(self):
		has = bool(self._members)
		self.memberList.Show(has)
		self.memberList.Enable(has)
		self.emptyLabel.Show(not has)
		if self._owner:
			self.removeButton.Show(has)
			self.removeButton.Enable(has)
		if has:
			self.memberList.Set([user_label(m) for m in self._members])
			if self._owner:
				self.memberList.CheckedItems = []
			self.memberList.SetSelection(0)
		self.Layout()

	def _run(self, action, users):
		"""action: remove_members or add_members."""
		options = {"do": action, "list_id": self._row["id"], "user_ids": [u["id"] for u in users]}
		callback = lambda result: self.on_done(result, action, users)
		if not self._plugin.start_job("list_do", callback, options, progress=False):
			ui.message(_("Still loading, please wait."))
			return
		self._busy = True
		if action == "remove_members":
			# Translators: Announced while members are being taken out of a list. {count} is how many.
			ui.message(_("Removing {count} member(s)...").format(count=len(users)))
		else:
			# Translators: Announced while members are being added to a list. {count} is how many.
			ui.message(_("Adding {count} member(s)...").format(count=len(users)))

	def on_remove(self, event=None):
		if self._busy or not self.removeButton.IsShown():
			return
		picked = [self._members[i] for i in self.memberList.CheckedItems if 0 <= i < len(self._members)]
		if not picked:
			# Translators: Announced when Remove checked is pressed with nothing checked, in the window that manages the members of a list.
			announce_now(_("No members checked."))
			return
		dlg = wx.MessageDialog(
			self,
			# Translators: Question before taking members out of a list. {names} is a comma separated list of user names.
			_("Remove {names} from this list?").format(names=member_names(picked)),
			# Translators: Title of the question before taking members out of a list.
			_("Confirm remove"), wx.YES_NO | wx.NO_DEFAULT,
		)
		try:
			if dlg.ShowModal() != wx.ID_YES:
				return
		finally:
			dlg.Destroy()
		self._run("remove_members", picked)

	def on_add_people(self, event=None):
		if self._busy:
			return
		dlg = AddPeopleDialog(self, self._plugin, {m["id"] for m in self._members})
		try:
			if dlg.ShowModal() != wx.ID_OK:
				return
			users = dlg.users
		finally:
			dlg.Destroy()
		if users:
			self._run("add_members", users)

	@safe_ui
	def on_done(self, result, action, users):
		self._busy = False
		if not result["ok"]:
			soundpack.play("error")
			ui.message(_("Action failed: {error}").format(error=result["message"]))
			return
		if result.get("account_id") != self._plugin.account_id():
			return
		by_id = {u["id"]: u for u in users}
		done = [by_id[i] for i in result["users"] if i in by_id]
		errors = ["@%s: %s" % (by_id[i]["handle"], text) for i, text in result["errors"] if i in by_id]
		if action == "remove_members":
			gone = {u["id"] for u in done}
			self._members = [m for m in self._members if m["id"] not in gone]
			self.delta -= len(done)
		else:
			have = {m["id"] for m in self._members}
			fresh = [u for u in done if u["id"] not in have]
			self._members.extend(fresh)
			self.delta += len(fresh)
		self._render()
		if done:
			if action == "remove_members":
				# Translators: Announced after members were taken out of a list. {names} is a comma separated list of user names.
				announce_now(_("Removed {names}.").format(names=member_names(done)))
			else:
				# Translators: Announced after members were added to a list. {names} is a comma separated list of user names.
				announce_now(_("Added {names}.").format(names=member_names(done)))
		if errors:
			if action == "remove_members":
				# Translators: Announced when some members could not be taken out of a list. {errors} is a semicolon separated list of "user: reason".
				ui.message(_("Some members could not be removed: {errors}").format(errors="; ".join(errors)))
			else:
				# Translators: Announced when some members could not be added to a list. {errors} is a semicolon separated list of "user: reason".
				ui.message(_("Some members could not be added: {errors}").format(errors="; ".join(errors)))


class FindListsDialog(wx.Dialog):
	"""The lists of another user: search for the user (only when the reader asks: Enter or Find), show their lists, then
	open one in a tab of its own (self.picked, a row, after OK) or look at who is in it. user: a user already chosen
	({"id", "handle", "name"}), which skips the search. X says "owned or followed" for the lists of a user and does not
	say who owns them, so the lists shown may include some the user only follows."""

	def __init__(self, parent, plugin, user=None):
		if user is None:
			# Translators: Title of the window that finds the lists of a user.
			title = _("Find lists by user")
		else:
			# Translators: Title of the window that shows the lists of one user. {user} is the user name.
			title = _("Lists of @{user}").format(user=user["handle"])
		super().__init__(parent, title=title, size=(500, 580))
		self._plugin = plugin
		self._dead = False
		self._user = user
		self._found = []   # the users the search found
		self._rows = []    # the lists of the user chosen
		self.picked = None

		sizer = wx.BoxSizer(wx.VERTICAL)
		row = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Label of the box that searches for a user in the window that finds the lists of a user.
		self.searchLabel = wx.StaticText(self, label=_("Search for a &user:"))
		row.Add(self.searchLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
		self.query = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
		row.Add(self.query, 1)
		# Translators: Button of the window that finds the lists of a user: searches for the user typed.
		self.findButton = wx.Button(self, label=_("&Find"))
		row.Add(self.findButton, 0, wx.LEFT, 5)
		sizer.Add(row, 0, wx.EXPAND | wx.ALL, 8)
		# Translators: Label of the list of the users found, in the window that finds the lists of a user.
		self.usersLabel = wx.StaticText(self, label=_("Users foun&d:"))
		self.users = wx.ListBox(self, choices=[], size=(-1, 100))
		# Translators: Button of the window that finds the lists of a user: loads the lists of the user highlighted.
		self.showButton = wx.Button(self, label=_("Show their &lists"))
		sizer.Add(self.usersLabel, 0, wx.LEFT | wx.RIGHT, 8)
		sizer.Add(self.users, 0, wx.EXPAND | wx.ALL, 8)
		sizer.Add(self.showButton, 0, wx.LEFT | wx.BOTTOM, 8)
		# Translators: Label of the list of the lists of the user, in the window that finds the lists of a user.
		self.listsLabel = wx.StaticText(self, label=_("&Their lists:"))
		self.lists = wx.ListBox(self, choices=[], size=(-1, 160))
		sizer.Add(self.listsLabel, 0, wx.LEFT | wx.RIGHT, 8)
		sizer.Add(self.lists, 1, wx.EXPAND | wx.ALL, 8)
		buttons = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Button of the window that finds the lists of a user: the list highlighted opens in a tab of its own.
		self.openButton = wx.Button(self, wx.ID_OK, _("Open in &new tab"))
		# Translators: Button of the window that finds the lists of a user: shows who is in the list highlighted.
		self.membersButton = wx.Button(self, label=_("&View members..."))
		# Translators: Button of the window that finds the lists of a user: follows the list highlighted. For a list that is followed already the button says Unfollow list instead.
		self.followButton = wx.Button(self, label=_("Follo&w list"))
		# Translators: Button that closes the window that finds the lists of a user.
		self.closeButton = wx.Button(self, wx.ID_CANCEL, _("&Close"))
		buttons.Add(self.openButton, 0, wx.RIGHT, 8)
		buttons.Add(self.membersButton, 0, wx.RIGHT, 8)
		buttons.Add(self.followButton, 0, wx.RIGHT, 8)
		buttons.Add(self.closeButton, 0)
		sizer.Add(buttons, 0, wx.ALIGN_CENTER | wx.BOTTOM, 8)
		self.SetSizer(sizer)
		self.SetEscapeId(wx.ID_CANCEL)
		self.CentreOnParent()

		self.query.Bind(wx.EVT_TEXT_ENTER, self.on_find)
		self.findButton.Bind(wx.EVT_BUTTON, self.on_find)
		self.showButton.Bind(wx.EVT_BUTTON, self.on_show_lists)
		self.openButton.Bind(wx.EVT_BUTTON, self.on_open)
		self.membersButton.Bind(wx.EVT_BUTTON, self.on_view_members)
		self.followButton.Bind(wx.EVT_BUTTON, self.on_follow)
		self.lists.Bind(wx.EVT_LISTBOX, lambda e: self._update_follow())
		self._show(("usersLabel", "users", "showButton"), False)
		self._show(("listsLabel", "lists", "openButton", "membersButton"), False)
		if user is None:
			self.query.SetFocus()
		else:
			self._show(("searchLabel", "query", "findButton"), False)
			self._load_lists()

	def Destroy(self):
		self._dead = True  # a result that arrives later is thrown away (see safe_ui)
		return super().Destroy()

	def _show(self, names, shown):
		for name in names:
			control = getattr(self, name)
			control.Show(shown)
			control.Enable(shown)
		if "lists" in names:
			self._update_follow()
		self.Layout()

	# ------------------------------------------------------------ the user

	def on_find(self, event=None):
		query = self.query.GetValue().strip()
		if not query:
			ui.message(_("Type something to search for."))
			self.query.SetFocus()
			return
		options = {
			"kind": "search", "params": {"handle": query, "query": query}, "chain": "listfind:%s" % query, "pages": 1,
		}
		if not self._plugin.start_job("users", self.on_found, options):
			ui.message(_("Still loading, please wait."))
			return
		ui.message(_("Loading, please wait."))

	@safe_ui
	def on_found(self, result):
		if not result["ok"]:
			soundpack.play("error")
			ui.message(result["message"])
			return
		self._found = result["users"]
		self._rows = []
		self._show(("listsLabel", "lists", "openButton", "membersButton"), False)
		if not self._found:
			self._show(("usersLabel", "users", "showButton"), False)
			ui.message(_("No users found."))
			self.query.SetFocus()
			return
		self.users.Set([user_label(u) for u in self._found])
		self._show(("usersLabel", "users", "showButton"), True)
		self.users.SetSelection(0)
		self.users.SetFocus()

	def on_show_lists(self, event=None):
		index = self.users.GetSelection()
		if not 0 <= index < len(self._found):
			return
		self._user = self._found[index]
		self._load_lists()

	# ------------------------------------------------------------ their lists

	def _load_lists(self):
		user = self._user
		callback = lambda result: self.on_lists(result, user)
		if not self._plugin.start_job("lists_of_user", callback, {"uid": user["id"]}):
			ui.message(_("Still loading, please wait."))
			return
		# Translators: Announced while the lists of a user are being loaded. {user} is the user name.
		announce_now(_("Loading lists of @{user}, please wait...").format(user=user["handle"]))

	@safe_ui
	def on_lists(self, result, user):
		if not result["ok"]:
			soundpack.play("error")
			ui.message(result["message"])
			return
		self._rows = result["items"]
		if not self._rows:
			self._show(("listsLabel", "lists", "openButton", "membersButton"), False)
			# Translators: Announced when a user has no lists. {user} is the user name.
			ui.message(_("@{user} has no lists.").format(user=user["handle"]))
			return
		self._set_labels()
		self._show(("listsLabel", "lists", "openButton", "membersButton"), True)
		self.lists.SetSelection(0)
		self._update_follow()  # choosing a list in code does not tell the box that its choice changed
		self.lists.SetFocus()
		# Translators: Announced after the lists of a user were loaded. {count} is how many.
		ui.message(_("{count} lists.").format(count=len(self._rows)))

	def _highlighted(self):
		index = self.lists.GetSelection()
		return self._rows[index] if 0 <= index < len(self._rows) else None

	def on_open(self, event=None):
		row = self._highlighted()
		if row is None:
			return
		self.picked = row
		self.EndModal(wx.ID_OK)

	def _set_labels(self):
		"""Who owns a list is not known, so the "followed" tag is only for the lists the account follows."""
		self.lists.Set([list_label(dict(r, owned=not r.get("following"))) for r in self._rows])

	def _update_follow(self):
		"""The Follow button shows with the list highlighted, and says what it will do."""
		row = self._highlighted() if self.lists.IsShown() else None
		self.followButton.Show(row is not None)
		self.followButton.Enable(row is not None)
		if row is not None:
			# Translators: Button of the window that finds the lists of a user: follows the list highlighted. For a list that is followed already the button says Unfollow list instead.
			self.followButton.SetLabel(_("Unfollo&w list") if row.get("following") else _("Follo&w list"))

	def on_follow(self, event=None):
		row = self._highlighted()
		if row is None:
			return
		index = self.lists.GetSelection()
		unfollow = bool(row.get("following"))
		if unfollow:
			dlg = wx.MessageDialog(
				self,
				# Translators: Question before unfollowing a list. {name} is the name of the list.
				_("Unfollow the list {name}?").format(name=row["name"]),
				# Translators: Title of the question before unfollowing a list.
				_("Unfollow list"), wx.YES_NO | wx.NO_DEFAULT,
			)
			try:
				if dlg.ShowModal() != wx.ID_YES:
					return
			finally:
				dlg.Destroy()
		options = {"do": "unfollow" if unfollow else "follow", "list_id": row["id"]}
		callback = lambda result: self.on_followed(result, index)
		if not self._plugin.start_job("list_do", callback, options):
			ui.message(_("Still loading, please wait."))
			return
		if unfollow:
			# Translators: Announced while a list is being unfollowed.
			announce_now(_("Unfollowing the list, please wait."))
		else:
			# Translators: Announced while a list is being followed.
			announce_now(_("Following the list, please wait."))

	@safe_ui
	def on_followed(self, result, index):
		if not result["ok"]:
			soundpack.play("error")
			ui.message(_("Action failed: {error}").format(error=result["message"]))
			return
		if result.get("account_id") != self._plugin.account_id() or not 0 <= index < len(self._rows):
			return
		item = result["item"]
		if self._rows[index]["id"] != item["id"]:
			return
		self._rows[index] = dict(self._rows[index], following=item["following"], members=item["members"], followers=item["followers"])
		self._set_labels()
		self.lists.SetSelection(index)
		self._update_follow()
		if item["following"]:
			# Translators: Announced after a list was followed.
			announce_now(_("List followed."))
		else:
			# Translators: Announced after a list that was followed was unfollowed.
			announce_now(_("List unfollowed."))

	def on_view_members(self, event=None):
		row = self._highlighted()
		if row is None:
			return
		options = {
			"kind": "list_members", "params": {"handle": "", "uid": row["id"]}, "chain": "list_members:" + row["id"],
			"pages": 1, "all": True,
		}
		callback = lambda result: self.on_members(result, row)
		if not self._plugin.start_job("users", callback, options):
			ui.message(_("Still loading, please wait."))
			return
		announce_now(_("Loading members, please wait."))

	@safe_ui
	def on_members(self, result, row):
		if not result["ok"]:
			soundpack.play("error")
			ui.message(result["message"])
			return
		dlg = ManageMembersDialog(self, self._plugin, dict(row, owned=False), result["users"])  # read only
		try:
			dlg.ShowModal()
		finally:
			dlg.Destroy()


def find_lists(main, plugin, user=None):
	"""The window that finds the lists of a user (user: already chosen, see FindListsDialog); the list picked opens in
	a tab of its own."""
	dlg = FindListsDialog(main, plugin, user)
	try:
		if dlg.ShowModal() != wx.ID_OK:
			return
		row = dlg.picked
	finally:
		dlg.Destroy()
	main.open_feed("list", row["id"], {"list_id": row["id"]}, row["name"])


class ListFeed(FeedTab):
	"""The tweets of the list selected in the Lists tab: a tab of tweets like a list opened as a tab of its own (same
	cache, same filter, same keys), built into the Lists tab and pointed at another list by show_list(). It is not one
	of the removable tabs: the Lists tab owns it."""

	SOURCE = "list"

	def __init__(self, parent, plugin):
		super().__init__(parent, plugin, {}, [], "", "")
		self._job_for = ""      # the list the running refresh was started for: an answer for another one is dropped
		self._older_for = ""
		self._focus_after = False

	def show_list(self, list_id, name):
		"""Show another list: what the old one kept goes first, then the cache of the new one. Nothing is asked of X."""
		self.flush_state()
		self.TAB_TEMP_KEY = list_id
		self.TAB_NAME = name
		self._params = {"list_id": list_id}
		self._focus_pending = None
		self._read_dirty = False
		self._load_filter(self._tab_aid)
		self._reset_list()
		started = time.perf_counter()  # TEMP
		posts = tabstate.get_cache(self._plugin.store, self._tab_aid, "list", list_id) or []
		cached = time.perf_counter()  # TEMP
		self._tried = bool(posts)
		self._show(posts, None)
		if time.perf_counter() - started > 0.05:  # TEMP
			log.info(TAG + "TEMP lists show_list: cache %.2fs (%d rows), show %.2fs" % (cached - started, len(posts), time.perf_counter() - cached))  # TEMP

	def clear(self):
		"""No list is selected (the account has none)."""
		self.show_list("", "")

	def reload_cache(self):
		"""The Lists tab is shown again: the same list opened as a tab of its own may have put newer tweets in the
		cache (a background check does). What was read here keeps its read mark."""
		if not self.TAB_TEMP_KEY:
			return
		posts = tabstate.get_cache(self._plugin.store, self._tab_aid, "list", self.TAB_TEMP_KEY) or []
		if {p.id for p in posts} == {p.id for p in self._all_rows()}:
			return
		self._show(posts, self._current_post_id())

	def _unread_changed(self, unread):
		if self.TAB_TEMP_KEY:
			super()._unread_changed(unread)

	def take_focus(self):
		"""Enter on a list in the tree: the tweets take the focus; a list that was never read is loaded first."""
		if len(self._posts):
			self.list.SetFocus()
		elif len(self._all_rows()):
			self.filter_btn.SetFocus()  # the filter hides every tweet: the way back is its button
		elif not self._tried:
			self._tried = True
			self._focus_after = True
			if not self.refresh():
				self._focus_after = False
				self._tried = False
		else:
			# Translators: Announced by Enter on a list in the Lists tab when the list has no tweets.
			ui.message(_("This list has no tweets."))

	def refresh(self, done=None, quiet=False):
		self._job_for = self.TAB_TEMP_KEY
		return super().refresh(done, quiet)

	@safe_ui
	def on_refreshed(self, result, done, quiet=False):
		focus, self._focus_after = self._focus_after, False
		if self._job_for != self.TAB_TEMP_KEY:  # another list was selected meanwhile: this answer is not for it
			if done:
				done(False)
			return
		super().on_refreshed(result, done, quiet)
		if focus and result["ok"] and len(self._posts):
			self.list.SetFocus()

	def onFetchOlder(self):
		self._older_for = self.TAB_TEMP_KEY
		super().onFetchOlder()

	@safe_ui
	def on_older_result(self, result):
		if self._older_for != self.TAB_TEMP_KEY:
			return
		super().on_older_result(result)


class ListsTab(wx.Panel):
	TAB_KEY = "lists"

	def __init__(self, parent, plugin):
		started_tab = time.perf_counter()  # TEMP
		super().__init__(parent)
		self._plugin = plugin
		self._aid = plugin.account_id()
		self._dead = False
		# Translators: Name of the Lists tab.
		self.TAB_NAME = _("Lists")
		# Translators: What the New button of the main window says in the Lists tab.
		self.NEW_LABEL = _("New list")
		self._lists = self._load_cache()  # rows of the lists, as the cache holds them (see service._list_row)
		self._current = None  # the row of the list shown
		# rows from before the cache held the owner and the counts are read again the first time the tab is shown
		self._tried = bool(self._lists) and all("owned" in r for r in self._lists)
		self._parked = False  # focus is waiting on the toolbar button because there is nothing to show
		self._quiet = 0  # >0: events come from our own code, not from the reader
		self._state_pending = False

		self._state_timer = wx.Timer(self)
		self.Bind(wx.EVT_TIMER, self._on_state_timer, self._state_timer)
		sizer = wx.BoxSizer(wx.VERTICAL)
		split = wx.BoxSizer(wx.HORIZONTAL)
		self.tree = wx.TreeCtrl(self, style=wx.TR_HAS_BUTTONS | wx.TR_HIDE_ROOT | wx.TR_SINGLE | wx.TR_LINES_AT_ROOT)
		# Translators: Accessible name of the list of lists of the Lists tab.
		self.tree.SetName(_("Lists"))
		# Translators: Hidden root of the list of lists in the Lists tab (read as its name).
		self._root = self.tree.AddRoot(_("Lists"))
		split.Add(self.tree, 1, wx.EXPAND | wx.RIGHT, 5)
		built = time.perf_counter()  # TEMP
		self.feed = ListFeed(self, plugin)
		feed_made = time.perf_counter()  # TEMP
		split.Add(self.feed, 2, wx.EXPAND)
		sizer.Add(split, 1, wx.EXPAND | wx.ALL, 8)
		# Translators: Button of the Lists tab that finds the lists of a user. The shortcut is added after the label.
		self.findButton = wx.Button(self, label=keymap.with_key(_("Find &lists by user..."), "list_find"))
		sizer.Add(self.findButton, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
		self.status = wx.StatusBar(self)
		# Translators: Accessible name of the status bar of the Lists tab.
		self.status.SetName(_("Lists status"))
		sizer.Add(self.status, 0, wx.EXPAND)
		self.SetSizer(sizer)

		self.tree.Bind(wx.EVT_TREE_SEL_CHANGED, self.on_selected)
		self.tree.Bind(wx.EVT_TREE_ITEM_ACTIVATED, self.on_activated)
		self.tree.Bind(wx.EVT_TREE_ITEM_MENU, self.on_menu)
		self.findButton.Bind(wx.EVT_BUTTON, lambda e: self.on_find_lists())
		self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)
		loading = time.perf_counter()  # TEMP
		self._load_lists(self._saved_state().get("list"))
		log.info(TAG + "TEMP lists tab: tree and buttons %.2fs, the tweet list %.2fs, the first list %.2fs" % (built - started_tab, feed_made - built, time.perf_counter() - loading))  # TEMP

	# ------------------------------------------------------------ the cache of the lists

	def _load_cache(self):
		raw = self._plugin.store.get_account_meta(self._aid, CACHE_KEY)
		try:
			data = json.loads(raw) if raw else []
		except ValueError:
			data = []
		return [d for d in data if isinstance(d, dict) and d.get("id")] if isinstance(data, list) else []

	def _save_cache(self):
		try:
			self._plugin.store.set_account_meta(self._aid, CACHE_KEY, json.dumps(self._lists))
		except Exception:
			log.error(TAG + "could not save the lists", exc_info=True)

	def _saved_state(self):
		try:
			state = json.loads(self._plugin.store.get_account_meta(self._aid, STATE_KEY) or "{}")
		except ValueError:
			return {}
		return state if isinstance(state, dict) else {}

	def _remember_soon(self):
		self._state_pending = True
		self._state_timer.StartOnce(STATE_SAVE_DELAY_MS)

	@safe_ui
	def _on_state_timer(self, event):
		self.flush_state()

	def flush_state(self):
		self._state_timer.Stop()
		if self._state_pending:
			self._state_pending = False
			try:
				state = {"list": self._current["id"] if self._current else None}
				self._plugin.store.set_account_meta(self._aid, STATE_KEY, json.dumps(state))
			except Exception:
				log.error(TAG + "could not save the place in the Lists tab", exc_info=True)
		self.feed.flush_state()

	# ------------------------------------------------------------ the tree

	def _child_items(self):
		items = []
		item, cookie = self.tree.GetFirstChild(self._root)
		while item.IsOk():
			items.append(item)
			item, cookie = self.tree.GetNextChild(self._root, cookie)
		return items

	def _by_id(self, list_id):
		return next((r for r in self._lists if r["id"] == list_id), None)

	def _render_tree(self):
		items = self._child_items()
		if [self.tree.GetItemData(i) for i in items] == [r["id"] for r in self._lists]:
			for item, row in zip(items, self._lists):  # the same lists: only the labels change
				if self.tree.GetItemText(item) != list_label(row):
					self.tree.SetItemText(item, list_label(row))
			return
		self._quiet += 1
		self.tree.Freeze()
		try:
			self.tree.DeleteAllItems()
			# Translators: Hidden root of the list of lists in the Lists tab (read as its name).
			self._root = self.tree.AddRoot(_("Lists"))
			for row in self._lists:
				item = self.tree.AppendItem(self._root, list_label(row))
				self.tree.SetItemData(item, row["id"])
		finally:
			self.tree.Thaw()
			self._quiet -= 1

	def _select_item(self, list_id):
		for item in self._child_items():
			if self.tree.GetItemData(item) == list_id:
				self._quiet += 1
				try:
					self.tree.SelectItem(item)
				finally:
					self._quiet -= 1
				return

	def _load_lists(self, keep_id=None):
		"""Draw the tree from the rows and show the tweets of a list: keep_id, else the one shown, else the first."""
		ids = [r["id"] for r in self._lists]
		wanted = keep_id or (self._current or {}).get("id")
		if wanted not in ids:
			wanted = ids[0] if ids else None
		self._render_tree()
		if wanted is None:
			self._current = None
			self.feed.clear()
		else:
			self._select_item(wanted)
			self._show(wanted)
		self._update_status()
		self._leave_toolbar_button()

	def _show(self, list_id):
		row = self._by_id(list_id)
		if row is None:
			return
		if self._current is not None and self._current["id"] == list_id:
			self._current = row  # the same list: its row may have changed
			return
		self._current = row
		self.feed.show_list(row["id"], row["name"])
		self._remember_soon()

	def _update_status(self):
		# Translators: Status bar of the Lists tab. {total} is the number of lists.
		self.status.SetStatusText(_("{total} lists").format(total=len(self._lists)))

	@safe_ui
	def on_selected(self, event):
		if not self._quiet:
			item = event.GetItem()
			if item.IsOk() and item != self._root:
				self._show(self.tree.GetItemData(item))
		event.Skip()

	@safe_ui
	def on_activated(self, event):
		"""Enter on a list: its tweets take the focus."""
		item = event.GetItem()
		if not item.IsOk() or item == self._root:
			return
		self._show(self.tree.GetItemData(item))
		self.feed.take_focus()

	def _check_boundary(self, key):
		item = self.tree.GetSelection()
		if not item.IsOk() or item == self._root:
			return
		sibling = self.tree.GetPrevSibling(item) if key == wx.WXK_UP else self.tree.GetNextSibling(item)
		if not sibling.IsOk():
			soundpack.play("boundary")

	# ------------------------------------------------------------ the menu of a list

	@safe_ui
	def on_menu(self, event):
		item = event.GetItem()
		if not item.IsOk() or item == self._root:
			return
		list_id = self.tree.GetItemData(item)
		if self._current is None or self._current["id"] != list_id:
			self._select_item(list_id)
			self._show(list_id)
		if self._current is not None:
			self._popup_menu(self._current)

	def _item(self, menu, label, callback):
		item = menu.Append(wx.ID_ANY, label)
		self.Bind(wx.EVT_MENU, lambda evt: callback(), item)

	def _popup_menu(self, row):
		menu = wx.Menu()
		owned = row.get("owned", True)
		if owned:
			# Translators: Entry of the menu of a list in the Lists tab. The shortcut is added after the label.
			self._item(menu, keymap.with_key(_("&Edit list..."), "list_edit"), lambda: self._edit(row))
		# Translators: Entry of the menu of a list in the Lists tab: who is in the list. The shortcut is added after the label.
		self._item(menu, keymap.with_key(_("&Manage members..."), "list_manage_members"), lambda: self._manage_members(row))
		# Translators: Entry of the menu of a list in the Lists tab: the tweets of the list in a tab of their own. The shortcut is added after the label.
		self._item(menu, keymap.with_key(_("Open in new &tab"), "list_open_tab"), lambda: self._open_tab(row))
		if owned:
			# Translators: Entry of the menu of a list in the Lists tab: deletes the list. The shortcut is added after the label.
			self._item(menu, keymap.with_key(_("&Remove list..."), "list_remove"), lambda: self._remove(row))
		else:
			# Translators: Entry of the menu of a list that the account follows but did not create: stops following it. The shortcut is added after the label.
			self._item(menu, keymap.with_key(_("&Unfollow list..."), "list_remove"), lambda: self._remove(row))
		self.PopupMenu(menu)
		menu.Destroy()

	@safe_ui
	def on_find_lists(self):
		"""The Find lists by user button and its key: the list picked opens in a tab of its own."""
		find_lists(self.GetTopLevelParent(), self._plugin)

	def _manage_members(self, row=None):
		"""Load every member of the list, then show them in the window that manages them."""
		row = row or self._current
		if row is None:
			ui.message(_("No list selected."))
			return
		options = {
			"kind": "list_members", "params": {"handle": "", "uid": row["id"]}, "chain": "list_members:" + row["id"],
			"pages": 1, "all": True,
		}
		callback = lambda result: self.on_members(result, row)
		if not self._plugin.start_job("users", callback, options):
			ui.message(_("Still loading, please wait."))
			return
		# Translators: Announced while the members of a list are being loaded.
		announce_now(_("Loading members, please wait."))

	@safe_ui
	def on_members(self, result, row):
		if not result["ok"]:
			soundpack.play("error")
			ui.message(result["message"])
			return
		if result.get("account_id") != self._aid:
			return
		dlg = ManageMembersDialog(self.GetTopLevelParent(), self._plugin, row, result["users"])
		try:
			dlg.ShowModal()
			delta = dlg.delta
		finally:
			dlg.Destroy()
		current = self._by_id(row["id"])  # the lists may have been read again meanwhile
		if delta and current is not None:
			current["members"] = max(0, (current.get("members") or 0) + delta)
			self._save_cache()
			self._render_tree()

	def _open_tab(self, row=None):
		row = row or self._current
		if row is None:
			ui.message(_("No list selected."))
			return
		self.GetTopLevelParent().open_feed("list", row["id"], {"list_id": row["id"]}, row["name"])

	def _start(self, options, text, action):
		"""Start a change to a list; text is said while it runs."""
		callback = lambda result: self.on_changed(result, action)
		if not self._plugin.start_job("list_do", callback, options):
			ui.message(_("Still loading, please wait."))
			return
		announce_now(text)

	@safe_ui
	def on_new(self, prefill=None):
		"""The New button of the main window and its key: in the Lists tab they make a list."""
		dlg = ListDialog(self.GetTopLevelParent())
		try:
			if dlg.ShowModal() != wx.ID_OK:
				return
			name, description, private = dlg.values()
		finally:
			dlg.Destroy()
		options = {"do": "create", "name": name, "description": description, "private": private}
		# Translators: Announced while a list is being created.
		self._start(options, _("Creating the list, please wait."), "create")

	def _edit(self, row=None):
		row = row or self._current
		if row is None:
			ui.message(_("No list selected."))
			return
		if not row.get("owned", True):
			# Translators: Announced when Edit list is used on a list the account follows but did not create.
			announce_now(_("You can only edit a list you created."))
			return
		dlg = ListDialog(self.GetTopLevelParent(), row)
		try:
			if dlg.ShowModal() != wx.ID_OK:
				return
			name, description, private = dlg.values()
		finally:
			dlg.Destroy()
		options = {"do": "edit", "list_id": row["id"], "name": name, "description": description, "private": private}
		# Translators: Announced while a list is being saved.
		self._start(options, _("Saving the list, please wait."), "edit")

	def _remove(self, row=None):
		row = row or self._current
		if row is None:
			ui.message(_("No list selected."))
			return
		if not row.get("owned", True):
			dlg = wx.MessageDialog(
				self.GetTopLevelParent(),
				# Translators: Question before unfollowing a list. {name} is the name of the list.
				_("Unfollow the list {name}?").format(name=row["name"]),
				# Translators: Title of the question before unfollowing a list.
				_("Unfollow list"), wx.YES_NO | wx.NO_DEFAULT,
			)
			try:
				if dlg.ShowModal() != wx.ID_YES:
					return
			finally:
				dlg.Destroy()
			# Translators: Announced while a list is being unfollowed.
			self._start({"do": "unfollow", "list_id": row["id"]}, _("Unfollowing the list, please wait."), "unfollow")
			return
		dlg = wx.MessageDialog(
			self.GetTopLevelParent(),
			# Translators: Question before deleting a list. {name} is the name of the list.
			_("Remove the list {name}? This can't be undone.").format(name=row["name"]),
			# Translators: Title of the question before deleting a list.
			_("Remove list"), wx.YES_NO | wx.NO_DEFAULT,
		)
		try:
			if dlg.ShowModal() != wx.ID_YES:
				return
		finally:
			dlg.Destroy()
		# Translators: Announced while a list is being deleted.
		self._start({"do": "delete", "list_id": row["id"]}, _("Removing the list, please wait."), "delete")

	@safe_ui
	def on_changed(self, result, action):
		"""The answer to a change of a list: the rows, the cache and the tree follow it."""
		if not result["ok"]:
			soundpack.play("error")
			# Translators: Announced when something done to a list failed. {error} is the reason.
			ui.message(_("Action failed: {error}").format(error=result["message"]))
			return
		if result.get("account_id") != self._aid:
			return
		keep = None
		if action in ("delete", "unfollow"):
			gone = result.get("list_id") or result["item"]["id"]
			self._lists = [r for r in self._lists if r["id"] != gone]
			store = self._plugin.store
			tabstate.delete_cache(store, self._aid, "list", gone)
			if (self._current or {}).get("id") == gone:
				self._current = None
			if action == "unfollow":
				# Translators: Announced after a list that was followed was unfollowed.
				text = _("List unfollowed.")
			else:
				# Translators: Announced after a list was deleted.
				text = _("List removed.")
		else:
			item = result["item"]
			keep = item["id"]
			if any(r["id"] == keep for r in self._lists):
				self._lists = [item if r["id"] == keep else r for r in self._lists]
				if (self._current or {}).get("id") == keep:
					self._current = item
					self.feed.TAB_NAME = item["name"]
				# Translators: Announced after a list was saved.
				text = _("List saved.")
			else:
				self._lists.append(item)
				# Translators: Announced after a list was created.
				text = _("List created.")
		self._save_cache()
		self._load_lists(keep)
		ui.message(text)

	# ------------------------------------------------------------ loading from X

	def refresh(self, done=None, quiet=False):
		"""Read the lists of the account again. With done, stay silent and call done(changed) at the end."""
		callback = lambda result: self.on_lists(result, done, quiet)
		if not self._plugin.start_job("lists", callback, {}, progress=not quiet, background=quiet):
			if done is None:
				ui.message(_("Still loading, please wait."))
			return False
		if done is None:
			ui.message(_("Loading lists, please wait."))
		return True

	@safe_ui
	def on_lists(self, result, done, quiet=False):
		if not result["ok"]:
			if not quiet:
				soundpack.play("error")
				ui.message(result["message"])
			if done:
				done(False)
			return
		if result.get("account_id") != self._aid:
			return
		rows = result["items"]
		changed = rows != self._lists
		self._lists = rows
		self._save_cache()
		self._load_lists()
		if done:
			done(changed)
			return
		ui.message(_("{count} lists.").format(count=len(rows)))

	def onCheckForUpdates(self, evt=None):
		"""The toolbar button: the lists, unless the focus is on the tweets of the list (F5 there reads them)."""
		focus = self.FindFocus()
		if focus is not None and self.feed.IsDescendant(focus):
			self.feed.onCheckForUpdates(None)
		else:
			self.refresh()

	# ------------------------------------------------------------ keyboard

	def on_char_hook(self, event):
		"""The keys of the tree of lists. Keys pressed in the tweets of a list are the tweet list's own."""
		if self.FindFocus() is not self.tree:
			event.Skip()
			return
		key = event.GetKeyCode()
		if key in (wx.WXK_UP, wx.WXK_DOWN) and not event.HasAnyModifiers():
			self._check_boundary(key)
		if keymap.matches(event, "check_updates"):
			self.refresh()
			return
		if keymap.matches(event, "list_edit"):
			self._edit()
			return
		if keymap.matches(event, "list_manage_members"):
			self._manage_members()
			return
		if keymap.matches(event, "list_find"):
			self.on_find_lists()
			return
		if keymap.matches(event, "list_open_tab"):
			self._open_tab()
			return
		if keymap.matches(event, "list_remove"):
			self._remove()
			return
		event.Skip()

	# ------------------------------------------------------------ what the main window asks of a tab

	def _current_post(self):
		"""The tweet in focus, for the suggestions of the search (Ctrl+F)."""
		return self.feed._current_post()

	def onTabActivated(self):
		ui.message(_("{name} tab").format(name=self.TAB_NAME))
		self._restoreFocusPosition()
		self.feed.reload_cache()
		if not self._tried and not self._lists:
			self._tried = True
			self.refresh()
		elif not self._tried:  # rows from an older version: read once for their owner and counts
			self._tried = True
			self.refresh(done=lambda changed: None, quiet=True)

	def _restoreFocusPosition(self, moveFocus=True):
		if not moveFocus:
			return
		if self._lists:
			self._parked = False
			self.tree.SetFocus()
		else:
			self._parked = True
			button = getattr(self.GetTopLevelParent(), "checkUpdatesButton", None)
			if button is not None:
				button.SetFocus()

	def _leave_toolbar_button(self):
		"""The tab was empty, so focus waited on the toolbar button: now it has lists, move it in."""
		if not self._parked or not self._lists:
			return
		button = getattr(self.GetTopLevelParent(), "checkUpdatesButton", None)
		try:
			if button is not None and self.IsShownOnScreen() and self.FindFocus() is button:
				self._parked = False
				self.tree.SetFocus()
		except RuntimeError:
			pass

	def reload_display(self):
		"""Settings changed: the tweets are drawn again."""
		self.feed.reload_display()
