var DB = [];
var DB_result = [];
var baseUrls = {};
var PER_PAGE = 30;
var isInitial = true;
var previousSearch = '';
var plistServerUrl = ''; // will append ?d=<data>
NodeList.prototype.forEach = Array.prototype.forEach; // fix for < iOS 9.3

/*
 * Init
 */

function setMessage(msg) {
    document.getElementById('content').innerHTML = msg;
}

function loadFile(url, onErrFn, fn) {
    try {
        const xhr = new XMLHttpRequest();
        xhr.open('GET', url, true);
        xhr.responseType = 'text';
        xhr.onload = function (e) { fn(e.target.response); };
        xhr.onerror = function (e) { onErrFn('Server or network error.'); };
        xhr.send();
    } catch (error) {
        onErrFn(error);
    }
}

function loadDB() {
    var config = null;
    try {
        config = loadConfig(true);
    } catch (error) {
        alert(error);
    }
    setMessage('Loading base-urls ...');
    loadFile('data/urls.json', setMessage, function (data) {
        baseUrls = JSON.parse(data);
        setMessage('Loading database ...');
        loadFile('data/apk.json', setMessage, function (data) {
            DB = JSON.parse(data);
            setMessage('ready. Links in database: ' + DB.length);
            if (config && (config.page > 0 || config.search || config.packageid)) {
                searchAPK(true);
            }
        });
    });
}

function loadConfig(chkServer) {
    if (!location.hash) {
        return; // keep default values
    }
    const params = location.hash.substring(1).split('&');
    const data = {};
    params.forEach(function (param) {
        const pair = param.split('=', 2);
        data[pair[0]] = decodeURIComponent(pair[1]);
    });
    document.querySelectorAll('input,select').forEach(function (input) {
        if (input.type === 'checkbox') {
            input.checked = data[input.id] || null;
        } else {
            input.value = data[input.id] || '';
        }
    });
    if (chkServer && data['plistServer']) {
        setPlistGen();
    }
    return data;
}

function saveConfig() {
    const data = [];
    document.querySelectorAll('input,select').forEach(function (e) {
        const value = e.type === 'checkbox' ? e.checked : e.value;
        if (value) {
            data.push(e.id + '=' + encodeURIComponent(value));
        }
    });
    const prev = location.hash;
    location.hash = '#' + data.join('&');
    return prev !== location.hash;
}

/*
 * Search
 */

function applySearch() {
    const term = document.getElementById('search').value.toLowerCase();
    const package_id = document.getElementById('packageid').value.trim().toLowerCase();
    const unique = document.getElementById('unique').checked;
    const minsdk = document.getElementById('minsdk').value;
    const minid = document.getElementById('minid').value;

    const minV = minsdk ? parseInt(minsdk) : 0;
    const minPK = minid ? parseInt(minid) : 0;

    // [pk, minSDK, title, packageId, version, baseUrl, pathName, size]
    DB_result = [];
    isInitial = false;
    const uniquePackageIds = {};
    DB.forEach(function (apk, i) {
        if (apk[1] < minV || apk[0] < minPK) {
            return;
        }
        if (package_id && apk[3].toLowerCase().indexOf(package_id) === -1) {
            return;
        }
        if (!term
            || apk[2].toLowerCase().indexOf(term) > -1
            || apk[3].toLowerCase().indexOf(term) > -1
            || apk[6].toLowerCase().indexOf(term) > -1
        ) {
            if (unique) {
                const pId = apk[3];
                if (uniquePackageIds[pId]) {
                    return;
                }
                uniquePackageIds[pId] = true;
            }
            DB_result.push(i);
        }
    });
    delete uniquePackageIds; // free up memory
}

function restoreSearch() {
    location.hash = previousSearch;
    const conf = loadConfig(false);
    previousSearch = '';
    if (conf.random) {
        randomAPK(conf.random);
    } else {
        searchAPK(true);
    }
}

function searchPackage(idx, additional) {
    previousSearch = location.hash + (additional || '');
    document.getElementById('packageid').value = DB[idx][3];
    document.getElementById('search').value = '';
    document.getElementById('page').value = null;
    document.getElementById('unique').checked = false;
    searchAPK();
}

function searchAPK(restorePage) {
    var page = 0;
    if (restorePage) {
        page = document.getElementById('page').value;
    } else {
        document.getElementById('page').value = null;
    }
    applySearch();
    printAPK((page || 0) * PER_PAGE);
    saveConfig();
}

/*
 * Random APK
 */

function urlsToImgs(redirectUrl, list) {
    const template = getTemplate('.screenshot');
    var rv = '<div class="carousel">';
    for (var i = 0; i < list.length; i++) {
        rv += renderTemplate(template, { $REF: list[i], $URL: redirectUrl + list[i] });
    }
    return rv + '</div>';
}

function randomAPK(specificId) {
    document.getElementById('search').value = '';
    document.getElementById('packageid').value = '';
    if (saveConfig() || isInitial || specificId) {
        applySearch();
    }
    var idx = specificId;
    if (!specificId) {
        if (DB_result.length > 0) {
            idx = DB_result[Math.floor(Math.random() * DB_result.length)];
        } else {
            idx = Math.floor(Math.random() * DB.length);
        }
    }
    const entry = entryToDict(DB[idx]);
    const output = document.getElementById('content');
    output.innerHTML = '<h3>Random:</h3>' + entriesToStr('.full', [idx]);
    output.lastElementChild.className += ' single';
    output.innerHTML += renderTemplate(getTemplate('.randomAction'), { $IDX: idx });

    if (!plistServerUrl) {
        output.innerHTML += getTemplate('.no-itunes');
        return;
    }
    // Append Play Store info to result
    const redirectUrl = plistServerUrl + '?r='
    const playStoreUrl = 'https://play.google.com/store/apps/details?id=' + entry.packageId;
    loadFile(redirectUrl + playStoreUrl, console.error, function (data) {
        // Play Store scraping would go here, simplified version:
        output.innerHTML += '<p class="no-play-store">Play Store integration available.</p>';
    });
}

/*
 * Output
 */

function versionToStr(version) {
    if (!version) { return '?'; }
    return version;
}

function strToVersion(versionStr) {
    return parseInt(versionStr) || 0;
}

function humanSize(size) {
    var sizeIndex = 0;
    while (size > 1024) {
        size /= 1024;
        sizeIndex += 1;
    }
    return size.toFixed(1) + ['kB', 'MB', 'GB'][sizeIndex];
}

function getTemplate(name) {
    return document.getElementById('templates').querySelector(name).outerHTML;
}

function renderTemplate(template, values) {
    return template.replace(/\$[A-Z]+/g, function (x) { return values[x]; });
}

function validUrl(url) {
    return encodeURI(url).replace('#', '%23').replace('?', '%3F');
}

function entryToDict(entry) {
    const pk = entry[0];
    return {
        pk: pk,
        minSDK: entry[1],
        title: entry[2],
        packageId: entry[3],
        version: entry[4],
        baseUrl: entry[5],
        pathName: entry[6],
        size: entry[7],
        apk_url: baseUrls[entry[5]] + '/' + entry[6],
        img_url: 'data/' + Math.floor(pk / 1000) + '/' + pk + '.jpg',
    }
}

function entriesToStr(templateType, data) {
    const template = getTemplate(templateType);
    var rv = '';
    for (var i = 0; i < data.length; i++) {
        const entry = entryToDict(DB[data[i]]);
        rv += renderTemplate(template, {
            $IDX: data[i],
            $IMG: entry.img_url,
            $TITLE: (entry.title || '?').replace('<', '&lt;'),
            $VERSION: entry.version,
            $PACKAGEID: entry.packageId,
            $MINSDK: entry.minSDK,
            $SIZE: humanSize(entry.size),
            $URLNAME: entry.pathName.split('/').slice(-1), // decodeURI
            $URL: validUrl(entry.apk_url),
        });
    }
    return rv;
}

function printAPK(offset) {
    if (!offset) { offset = 0; }

    const total = DB_result.length;
    var content = '<h3>Results: ' + total;
    if (previousSearch) {
        content += ' -- Go to: <a onclick="restoreSearch()">previous search</a>';
    }
    content += '</h3>';
    const page = Math.floor(offset / PER_PAGE);
    const pages = Math.ceil(total / PER_PAGE);
    if (pages > 1) {
        content += paginationShort(page, pages);
    }

    const templateType = document.getElementById('unique').checked ? '.short' : '.entry';
    content += entriesToStr(templateType, DB_result.slice(offset, offset + PER_PAGE));

    if (pages > 1) {
        content += paginationShort(page, pages);
        content += paginationFull(page, pages);
    }

    document.getElementById('content').innerHTML = content;
    window.scrollTo(0, 0);
}

/*
 * Pagination
 */

function p(page) {
    printAPK(page * PER_PAGE);
    document.getElementById('page').value = page || null;
    saveConfig();
}

function paginationShort(page, pages) {
    return '<div class="shortpage">'
        + '<button onclick="p(' + (page - 1) + ')" ' + (page == 0 ? 'disabled' : '') + '>Prev</button>'
        + '<span>' + (page + 1) + ' / ' + pages + '</span>'
        + '<button onclick="p(' + (page + 1) + ')" ' + (page + 1 == pages ? 'disabled' : '') + '>Next</button>'
        + '</div>';
}

function paginationFull(page, pages) {
    var rv = '<div id="pagination">Pages:';
    for (var i = 0; i < pages; i++) {
        if (i === page) {
            rv += '\n<b>' + (i + 1) + '</b>';
        } else {
            rv += '\n<a onclick="p(' + i + ')">' + (i + 1) + '</a>';
        }
    }
    return rv + '</div>';
}

/*
 * Install on Android Device
 */

function setPlistGen() {
    const testURL = document.getElementById('plistServer').value;
    const scheme = testURL.slice(0, 7);
    if (scheme != 'http://' && scheme != 'https:/') {
        alert('URL must start with http:// or https://.');
        return;
    }
    loadFile(testURL + '?d=' + btoa('{"u":"1"}'), alert, function (data) {
        plistServerUrl = testURL;
        document.getElementById('overlay').hidden = true;
        saveConfig();
    });
}

function urlWithSlash(url) {
    return url.toString().slice(-1) === '/' ? url : (url + '/');
}

function utoa(data) {
    return btoa(unescape(encodeURIComponent(data)));
}

function installAPK(idx) {
    if (!plistServerUrl) {
        document.getElementById('overlay').hidden = false;
        return;
    }
    const thisServerUrl = location.href.replace(location.hash, '');
    const entry = entryToDict(DB[idx]);
    const json = JSON.stringify({
        u: validUrl(entry.apk_url),
        n: entry.title,
        p: entry.packageId,
        v: entry.version.split(' ')[0],
        i: urlWithSlash(thisServerUrl) + entry.img_url,
    }, null, 0)
    var b64 = '';
    try {
        b64 = btoa(json);
    } catch (error) {
        b64 = utoa(json);
    }
    while (b64.slice(-1) === '=') {
        b64 = b64.slice(0, -1);
    }
    // Direct download link for APK
    window.location.href = validUrl(entry.apk_url);
}
