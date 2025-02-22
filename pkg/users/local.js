/*
 * This file is part of Cockpit.
 *
 * Copyright (C) 2013 Red Hat, Inc.
 *
 * Cockpit is free software; you can redistribute it and/or modify it
 * under the terms of the GNU Lesser General Public License as published by
 * the Free Software Foundation; either version 2.1 of the License, or
 * (at your option) any later version.
 *
 * Cockpit is distributed in the hope that it will be useful, but
 * WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
 * Lesser General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public License
 * along with Cockpit; If not, see <http://www.gnu.org/licenses/>.
 */
import '../lib/patternfly/patternfly-4-cockpit.scss';
import 'polyfills'; // once per application
import 'cockpit-dark-theme'; // once per page

import cockpit from 'cockpit';
import React, { useState, useEffect } from 'react';
import { createRoot } from 'react-dom/client';
import { superuser } from "superuser";

import { usePageLocation, useLoggedInUser, useFile } from "hooks.js";
import { etc_passwd_syntax, etc_group_syntax } from "./parsers.js";
import { AccountsMain } from "./accounts-list.js";
import { AccountDetails } from "./account-details.js";

superuser.reload_page_on_change();

export const admins = ['sudo', 'root', 'wheel'];
const sortGroups = groups => {
    return groups.sort((a, b) => {
        if (a.isAdmin)
            return -1;
        if (b.isAdmin)
            return 1;
        if (a.members === b.members)
            return a.name.localeCompare(b.name);
        else
            return b.members - a.members;
    });
};

function AccountsPage() {
    const { path } = usePageLocation();
    const accounts = useFile("/etc/passwd", { syntax: etc_passwd_syntax });
    const shadow = useFile("/etc/shadow", { superuser: true });
    const groups = useFile("/etc/group", { syntax: etc_group_syntax });
    const current_user_info = useLoggedInUser();

    const [details, setDetails] = useState(null);
    useEffect(() => {
        getLogins().then(setDetails);

        // Watch `/var/run/utmp` to register when user logs in or out
        const handle = cockpit.file("/var/run/utmp", { superuser: "try", binary: true });
        handle.watch(() => {
            getLogins().then(setDetails);
        });
        return handle.close;
    }, [path, accounts, shadow]);

    // lastlog uses same sorting as /etc/passwd therefore arrays can be combined based on index
    let accountsInfo = [];
    if (accounts && details)
        accountsInfo = accounts.map((account, i) => {
            return Object.assign({}, account, details[i]);
        });

    const groupsExtraInfo = sortGroups(
        (groups || []).map(group => {
            const userlistPrimary = accountsInfo.filter(account => account.gid === group.gid).map(account => account.name);
            const userlist = group.userlist.filter(el => el !== "");
            return ({ ...group, userlistPrimary, userlist, members: userlist.length + userlistPrimary.length, isAdmin: admins.includes(group.name) });
        })
    );

    if (path.length === 0) {
        return <AccountsMain accountsInfo={accountsInfo} current_user={current_user_info && current_user_info.name} groups={groupsExtraInfo || []} />;
    } else {
        return (
            <AccountDetails accounts={accountsInfo} groups={groupsExtraInfo || []} shadow={shadow || []}
                            current_user={current_user_info && current_user_info.name} user={path[0]} />
        );
    }
}

function get_locked(name) {
    return cockpit.spawn(["/usr/bin/passwd", "-S", name], { environ: ["LC_ALL=C"], superuser: "require" })
            .catch(() => "")
            .then(content => {
                const status = content.split(" ")[1];
                // libuser uses "LK", shadow-utils use "L".
                return status && (status == "LK" || status == "L");
            });
}

async function getLogins() {
    const lastlog = await cockpit.spawn(["/usr/bin/lastlog"], { environ: ["LC_ALL=C"] })
            .catch(err => console.warn("Unexpected error when getting last login information", err));
    const w = await cockpit.spawn(["/usr/bin/w", "-sh"], { environ: ["LC_ALL=C"] })
            .catch(err => console.warn("Unexpected error when getting logged in accounts", err));

    const currentLogins = w.split('\n').slice(0, -1).map(line => {
        return line.split(/ +/)[0];
    });

    // drop header and last empty line with slice
    const promises = lastlog.split('\n').slice(1, -1).map(async line => {
        const splitLine = line.split(/ +/);
        const name = splitLine[0];
        const isLocked = await get_locked(name);

        if (line.indexOf('**Never logged in**') > -1) {
            return new Promise(resolve => {
                resolve({ name: name, loggedIn: false, lastLogin: null, isLocked: isLocked });
            });
        }

        const date_fields = splitLine.slice(-5);
        // this is impossible to parse with Date() (e.g. Firefox does not work with all time zones), so call `date` to parse it
        return cockpit.spawn(["date", "+%s", "-d", date_fields.join(' ')], { environ: ["LC_ALL=C"], err: "out" })
                .then(out => {
                    return { name: name, loggedIn: currentLogins.includes(name), lastLogin: parseInt(out) * 1000, isLocked: isLocked };
                })
                .catch(e => console.warn(`Failed to parse date from lastlog line '${line}': ${e.toString()}`));
    });

    return Promise.all(promises);
}

function init() {
    const root = createRoot(document.getElementById("page"));
    root.render(<AccountsPage />);
    document.body.removeAttribute("hidden");
}

document.addEventListener("DOMContentLoaded", init);
