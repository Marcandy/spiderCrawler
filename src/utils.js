const fs = require('fs');

const generateTitle = (product, link) => {

    const title = product
        .find('.a-size-medium.a-color-base.a-text-normal')
        .text();

    if (title === '') {
        const urlSegement = link;
        if (typeof urlSegment === 'string') {
            const urlSegmentStripped = urlSegement.split('/')[1];
            const title = urlSegmentStripped.replace(/-/g, ' ' );
            return title;
        }
    }
    return title;
}


const generateFilename = ( ) => {
    const date = new Date();
    const year = date.getFullYear();
    const month = date.getMonth() + 1;
    const day = date.getDay();
    const hour = date.getHours();
    const minute = date.getMinutes();
    const second = date.getSeconds();

    const filename = `${year}-${month}-${day}-${hour}-${minute}-${second}.json`;

    return filename
}


const saveProductJson = (products) => {
    const filename = generateFilename();

    const jsonProducts = JSON.stringify(products, null, 2);

    const folder  = process.env.NODE_ENV === 'test' ? 'test-data' : 'data';

    if (!fs.existsSync(folder)) {
        fs.mkdirSync(folder);
    }

    fs.writeFileSync(`${folder}/${filename}`, jsonProducts);

    return `./data/${filename}`; 
};

module.exports = {
    generateTitle,
    generateFilename,
    saveProductJson
};